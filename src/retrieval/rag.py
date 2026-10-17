"""RAG 模块（Embedding + BM25 + RRF 融合 + 两级 Rerank）

整体流水线：
    knowledge/*.md --加载--> 原始文档 --切分--> chunks
        --embedding-3向量化--> ChromaDB(持久化 ./chroma_db) --向量检索--> candidates
        --BM25关键词匹配--> top_k 文档
    两路结果 --RRF融合--> 粗排 Top-20 --两级Rerank--> Top-5 片段

功能增强：
- 配置收口到 config.py（类型安全）
- 新增 embedding API 熔断器（circuit breaker）：连续 N 次失败后短路 30s
- 结构化日志替换 print()

增强：
- 新增 BM25 关键词检索（src/retrieval/bm25.py）
- 新增两级 Rerank（src/retrieval/reranker.py）
- Hybrid Search：BM25 + Embedding 的 RRF 融合
- 降级策略：Embedding 不可用时退化为纯 BM25，BM25 不可用时退化为纯向量检索

设计决定：
- 不用 LangChain 的 DocumentLoader / TextSplitter / VectorStore 封装
  只用 chromadb 原生 API —— 每个环节自己写，每个参数都要能讲清楚为什么。
- Embedding 走 OpenAI 兼容的 POST /embeddings 接口（智谱 embedding-3）
- build_knowledge_base() 幂等：集合里已有数据直接跳过
- search() 永不抛异常：知识库为空、检索无结果、embedding 接口报错，
  都降级为一段明确的文字返回给 Agent
"""

import logging
import os
import time
from enum import Enum
from functools import lru_cache
from typing import Any

import chromadb
import httpx
from dotenv import load_dotenv

from src.config.settings import settings

load_dotenv()

logger = logging.getLogger(__name__)

# ---------- 路径与配置 ----------
# 本文件位于 src/retrieval/，所以项目根目录要往上退两级，否则会把
# src/knowledge、src/retrieval/chroma_db 当成数据目录（历史踩坑点）。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
KNOWLEDGE_DIR = os.path.join(PROJECT_ROOT, "knowledge")
# chroma_path 允许配绝对路径（Docker 卷，如 /opt/app/chroma_db），相对路径则相对项目根目录
CHROMA_DIR = (
    settings.chroma_path
    if os.path.isabs(settings.chroma_path)
    else os.path.join(PROJECT_ROOT, settings.chroma_path)
)
COLLECTION_NAME = "knowledge_base"

# Embedding 配置
EMBEDDING_MODEL_NAME = settings.embedding_model_name
EMBEDDING_BASE_URL = (settings.embedding_base_url or settings.llm_base_url or "").rstrip("/")
EMBEDDING_API_KEY = settings.embedding_api_key or settings.llm_api_key
EMBEDDING_DIMENSIONS = settings.embedding_dimensions

# 切分参数
CHUNK_SIZE = 350
CHUNK_OVERLAP = 60

# 相似度阈值
SIMILARITY_THRESHOLD = float(settings.embedding_similarity_threshold)

# 熔断器配置
EMBEDDING_FAILURE_THRESHOLD = settings.embedding_failure_threshold
EMBEDDING_RECOVERY_TIMEOUT = settings.embedding_recovery_timeout

# BM25 + Rerank 配置
BM25_TOP_K = 15  # BM25 召回条数
RRF_K = 60  # RRF 融合参数 k
RRERANK_TOP_K_COARSE = 20  # 第一级粗筛保留条数
RERANK_TOP_K_FINAL = 5  # 第二级精排最终条数

NOT_FOUND_MSG = "未找到相关信息"
KB_NOT_BUILT_MSG = "知识库尚未建立，请先运行 python scripts/build_kb.py 建库"

# 进程内缓存 collection 句柄（lru_cache 单例，见 get_collection）
_bm25_retriever = None


# ---------- Embedding API 熔断器 ----------
class _CircuitState(Enum):
    CLOSED = "closed"  # 正常
    OPEN = "open"  # 熔断
    HALF_OPEN = "half_open"  # 探活恢复


_circuit: dict[str, Any] = {
    "state": _CircuitState.CLOSED,
    "last_failure_time": 0.0,
    "consecutive_failures": 0,
}


def _check_circuit() -> None:
    """检查熔断器状态，OPEN 且未过恢复期时抛出 RuntimeError。"""
    state = _circuit["state"]
    now = time.time()

    if state == _CircuitState.OPEN:
        elapsed = now - _circuit["last_failure_time"]
        if elapsed > EMBEDDING_RECOVERY_TIMEOUT:
            _circuit["state"] = _CircuitState.HALF_OPEN
            logger.info("embedding 熔断器进入 HALF_OPEN 状态，允许探测请求")
        else:
            remaining = int(EMBEDDING_RECOVERY_TIMEOUT - elapsed)
            raise RuntimeError(
                f"Embedding API 熔断器处于 OPEN 状态（服务可能宕机），还需等待 {remaining}s 后重试"
            )
    elif state == _CircuitState.HALF_OPEN:
        logger.info("embedding 熔断器处于 HALF_OPEN 状态，放行探测请求")


def _record_circuit_success() -> None:
    """成功时重置熔断器状态。"""
    _circuit["consecutive_failures"] = 0
    _circuit["state"] = _CircuitState.CLOSED


def _record_circuit_failure() -> None:
    """失败时累加计数，达到阈值则打开熔断器。"""
    _circuit["consecutive_failures"] += 1
    _circuit["last_failure_time"] = time.time()
    if _circuit["consecutive_failures"] >= EMBEDDING_FAILURE_THRESHOLD:
        _circuit["state"] = _CircuitState.OPEN
        logger.warning(
            f"Embedding API 连续失败 {_circuit['consecutive_failures']} 次，"
            f"触发熔断，{EMBEDDING_RECOVERY_TIMEOUT}s 后恢复"
        )


def load_documents() -> list[dict]:
    """扫描 knowledge/ 目录下所有 .md / .txt 文件，返回 [{source, text}]。

    source 用文件名（不含扩展名），它就是最后拼进答案里的"来源标注"。
    """
    docs = []
    for filename in sorted(os.listdir(KNOWLEDGE_DIR)):
        if not filename.lower().endswith((".md", ".txt")):
            continue
        path = os.path.join(KNOWLEDGE_DIR, filename)
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            docs.append({"source": os.path.splitext(filename)[0], "text": text})
    logger.info("加载 %d 篇知识文档", len(docs))
    return docs


# ---------- 2. 文本切分 ----------
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把一篇文档切成若干个 chunk。

    策略分两层：
    1. 先按空行 / markdown 标题切成"自然段"，语义边界天然在这里；
    2. 贪心地合并自然段：不超过 chunk_size 就继续装，装不下就开新 chunk；
    3. 如果单个自然段本身就超过 chunk_size（罕见），用滑动窗口硬切，
       窗口步长 = chunk_size - overlap，保证相邻窗口有 overlap 的重叠。

    这是"结构感知 + 长度兜底"的朴素做法。对 300 字左右的政策类短文档，
    大多数文档只会产出 1 个 chunk，这是符合预期的：一篇短文本来就是一个语义单元。
    """
    # 把标题行和空行都当作段落分隔符
    paragraphs: list[str] = []
    for block in text.replace("\r\n", "\n").split("\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):  # markdown 标题行单独成段，方便归入下文
            if paragraphs and paragraphs[-1] != "":
                paragraphs.append("")
            paragraphs.append(block.lstrip("#").strip())
            paragraphs.append("")
        else:
            paragraphs.append(block)

    # 重新按空行聚合出自然段
    natural: list[str] = []
    buf: list[str] = []
    for p in paragraphs:
        if p == "":
            if buf:
                natural.append("\n".join(buf))
                buf = []
        else:
            buf.append(p)
    if buf:
        natural.append("\n".join(buf))

    # 贪心合并自然段
    chunks: list[str] = []
    current = ""
    for para in natural:
        if not current:
            current = para
        elif len(current) + len(para) + 1 <= chunk_size:
            current = current + "\n" + para
        else:
            chunks.append(current)
            current = para
        # 单段超长的兜底：滑动窗口硬切
        step = chunk_size - overlap
        while len(current) > chunk_size and step > 0:
            chunks.append(current[:chunk_size])
            current = current[step:]
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


# ---------- 3. Embedding 调用 ----------
def embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 Embedding API，带熔断器保护，超时/错误时抛出带信息的 RuntimeError。"""
    if not EMBEDDING_API_KEY or not EMBEDDING_BASE_URL:
        raise RuntimeError("缺少 embedding 配置，无法调用向量接口")
    _check_circuit()

    url = f"{EMBEDDING_BASE_URL}/embeddings"
    headers = {"Authorization": f"Bearer {EMBEDDING_API_KEY}"}

    all_vectors: list[list[float] | None] = [None] * len(texts)
    start = 0
    while start < len(texts):
        batch_end = min(start + 32, len(texts))
        batch = texts[start:batch_end]
        payload = {
            "model": EMBEDDING_MODEL_NAME,
            "input": batch,
            "dimensions": EMBEDDING_DIMENSIONS,
        }
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        except httpx.TimeoutException as e:
            _record_circuit_failure()
            raise RuntimeError(
                f"Embedding API 超时（batch 起始位置 {start}，model={EMBEDDING_MODEL_NAME}）"
            ) from e
        except httpx.RequestError as e:
            _record_circuit_failure()
            raise RuntimeError(f"Embedding API 请求失败（batch 起始位置 {start}）: {e}") from e

        if resp.status_code != 200:
            _record_circuit_failure()
            raise RuntimeError(
                f"embedding 接口返回 {resp.status_code}（model={EMBEDDING_MODEL_NAME}，"
                f"base_url={EMBEDDING_BASE_URL}）：{resp.text[:300]}"
            )

        data = resp.json()
        if "data" in data:
            for item in data["data"]:
                all_vectors[start + item["index"]] = item["embedding"]
        else:
            for i, vec in enumerate(data.get("embeddings", [])):
                all_vectors[start + i] = vec
        start = batch_end

    _record_circuit_success()
    return all_vectors  # type: ignore[return-value]


# ---------- 4. ChromaDB 操作 ----------
@lru_cache(maxsize=1)
def get_collection() -> Any:
    """获取或创建 ChromaDB 集合（lru_cache 单例）。

    只用 lru_cache 一层缓存：此前另有一个模块级 `_collection_cache`，
    重建知识库时 `cache_clear()` 只清了 lru，模块变量仍持有已删除的旧
    collection 对象，`--rebuild` 路径后续 add 会报「collection does not exist」。
    """
    os.makedirs(CHROMA_DIR, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


# ---------- 5. 建库 ----------
def build_knowledge_base(rebuild: bool = False) -> dict:
    """加载知识文档 → 切分 → 向量化 → 写入 ChromaDB。

    幂等逻辑：集合里已有数据且未指定 rebuild 时直接跳过。
    返回统计信息 {files, chunks, skipped}。
    """
    docs = load_documents()
    if not docs:
        raise RuntimeError(f"knowledge/ 目录下没有找到任何 .md/.txt 文档：{KNOWLEDGE_DIR}")

    collection = get_collection()
    if collection.count() > 0 and not rebuild:
        logger.info(
            "知识库已存在（%d 个 chunk），跳过建库。如需重建：python scripts/build_kb.py --rebuild",
            collection.count(),
        )
        return {"files": len(docs), "chunks": collection.count(), "skipped": True}

    if rebuild:
        client = collection._client  # noqa: SLF001
        client.delete_collection(COLLECTION_NAME)
        get_collection.cache_clear()
        collection = get_collection()

    texts: list[str] = []
    metadatas: list[dict] = []
    for doc in docs:
        for i, chunk in enumerate(chunk_text(doc["text"])):
            texts.append(chunk)
            metadatas.append({"source": doc["source"], "chunk_index": i})

    logger.info("加载 %d 篇文档，切分出 %d 个 chunk，开始向量化...", len(docs), len(texts))
    vectors = embed_texts(texts)

    collection.add(
        ids=[f"{m['source']}::{i}" for i, m in enumerate(metadatas)],
        embeddings=vectors,
        documents=texts,
        metadatas=metadatas,
    )
    logger.info("建库完成：%d 个 chunk 已写入 %s", collection.count(), CHROMA_DIR)

    # 初始化 BM25 索引
    _init_bm25(texts, [m["source"] for m in metadatas])
    return {"files": len(docs), "chunks": len(texts), "skipped": False}


# ---------- 6. BM25 初始化 ----------
def _init_bm25(texts: list[str], sources: list[str]) -> None:
    """构建 BM25 索引（建库时调用一次）。"""
    global _bm25_retriever
    from src.retrieval.bm25 import BM25Retriever

    _bm25_retriever = BM25Retriever()
    _bm25_retriever.build(texts, sources)
    logger.info("BM25 索引构建完成，覆盖 %d 个 chunk", len(texts))


def _ensure_bm25(collection: Any) -> None:
    """确保 BM25 索引可用：进程内没建过就从向量库里把 chunk 回灌重建。

    为什么必须有这一步：`_init_bm25` 只在 `build_knowledge_base()` 里调用，
    而线上服务是「先跑 build_kb.py 建库、再起 uvicorn」两个进程。服务进程里
    `_bm25_retriever` 永远是 None，`search()` 只走向量检索——README 里的
    Hybrid Search 在实际运行的服务上从来没有生效过。

    这里用 collection.get() 一次性把文档取回来重建索引（几百篇短文档，
    毫秒级），失败则静默降级为纯向量检索，不影响主链路。
    """
    if _bm25_retriever is not None:
        return
    try:
        data = collection.get(include=["documents", "metadatas"])
        documents = [d for d in (data.get("documents") or []) if d]
        if not documents:
            return
        metadatas = data.get("metadatas") or []
        sources = [
            (metadatas[i] or {}).get("source", "未知来源") if i < len(metadatas) else "未知来源"
            for i in range(len(documents))
        ]
        _init_bm25(documents, sources)
    except Exception as e:  # 懒加载失败不能影响检索主链路
        logger.warning("BM25 索引懒加载失败，本次降级为纯向量检索: %s", e)


def _get_bm25_results(query: str, top_k: int = BM25_TOP_K) -> list[tuple[str, str, float]]:
    """获取 BM25 检索结果，失败时静默降级。返回 [(document, source, score)]。"""
    if _bm25_retriever is None:
        return []
    try:
        return _bm25_retriever.search(query, top_k=top_k)
    except Exception as e:
        logger.warning("BM25 检索异常，降级为纯向量检索: %s", e)
        return []


# ---------- 7. RRF 融合 ----------
def _rrf_fuse(
    vector_results: list[tuple[float, str, str]],
    bm25_results: list[tuple[str, str, float]],
    k: int = RRF_K,
) -> list[tuple[float | None, str, str, float]]:
    """Reciprocal Rank Fusion 融合两路检索结果。

    公式：score(doc) = sum(1 / (k + rank_i)) for each retrieval list containing doc
    返回 [(vector_similarity, source, document, rrf_score), ...]

    vector_similarity 为 None 表示该文档只被 BM25 召回（没有向量分数）。
    以前这里统一填 0.0，下游 reranker 用 `similarity > 0.3` 过滤时会把
    BM25 独有结果全部丢掉 —— 于是「embedding 挂了降级为纯 BM25」实际返回
    「未找到相关信息」。用 None 把「没有向量分」和「向量分很低」区分开。
    """
    from collections import defaultdict

    rrf_scores: dict[str, float] = defaultdict(float)
    doc_info: dict[str, tuple[float | None, str]] = {}  # doc_text -> (similarity, source)

    # 向量检索结果（已按相似度排序）
    for rank, (sim, source, doc) in enumerate(vector_results, start=1):
        rrf_scores[doc] += 1.0 / (k + rank)
        doc_info[doc] = (sim, source)

    # BM25 结果：来源用文档自己的来源，而不是塞一个字面量 "bm25"
    for rank, (doc, source, _score) in enumerate(bm25_results, start=1):
        rrf_scores[doc] += 1.0 / (k + rank)
        if doc not in doc_info:
            doc_info[doc] = (None, source)

    # 按 RRF 分数排序
    fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return [(doc_info[doc][0], doc_info[doc][1], doc, rrf_score) for doc, rrf_score in fused]


# ---------- 8. 主搜索入口 ----------
def search(query: str, top_k: int = RERANK_TOP_K_FINAL) -> str:
    """混合检索：向量 + BM25 RRF 融合 → 两级 Rerank → 返回 top_k 条片段。

    返回给上层的是一段带来源标注的纯文本上下文，Agent 拿到后
    组织成自然语言回复。注意这个函数永远不抛异常：
    - 知识库没建 / 为空   -> 提示先建库
    - 检索结果为空        -> "未找到相关信息"
    - embedding 接口挂了  -> 用 BM25 结果兜底（RRF 里保留「无向量分」的候选）
    - BM25 不可用        -> 自动降级为纯向量检索

    这样 Agent 在任何情况下都能把结果继续往下说，而不是整条链路 500。
    """
    try:
        collection = get_collection()
        if collection.count() == 0:
            return KB_NOT_BUILT_MSG

        # --- 第 1 步：向量检索 ---
        vector_hits: list[tuple[float, str, str]] = []
        try:
            query_vector = embed_texts([query])[0]
            result = collection.query(
                query_embeddings=[query_vector],
                n_results=min(BM25_TOP_K * 2, collection.count()),
                include=["documents", "metadatas", "distances"],
            )
            documents = result["documents"][0]
            if documents:
                for doc, meta, dist in zip(
                    documents, result["metadatas"][0], result["distances"][0]
                ):
                    similarity = 1 - dist
                    if similarity >= SIMILARITY_THRESHOLD:
                        vector_hits.append((similarity, meta.get("source", "未知来源"), doc))
        except Exception as e:
            logger.warning("向量检索异常，降级: %s", e)

        # --- 第 2 步：BM25 检索 ---
        # 先确保索引存在：服务进程启动后直接检索时，索引还没建过（见 _ensure_bm25）
        _ensure_bm25(collection)
        bm25_hits = _get_bm25_results(query)

        # --- 第 3 步：RRF 融合 ---
        fused = _rrf_fuse(vector_hits, bm25_hits)
        if not fused:
            return NOT_FOUND_MSG

        # --- 第 4 步：两级 Rerank ---
        from src.retrieval.reranker import check_conflicts, two_stage_rerank

        # 不再伪造 chunk_index=0：reranker 不用这个字段，留着只会误导
        candidates = [
            {"document": doc, "source": source}
            for _, source, doc, _ in fused[:RRERANK_TOP_K_COARSE]
        ]
        similarities = [sim for sim, _, _, _ in fused[:RRERANK_TOP_K_COARSE]]
        # top_k 之前是"接了但没用"的摆设参数，最终条数被写死成常量；现在真的生效
        reranked = two_stage_rerank(candidates, similarities, top_k_final=max(1, int(top_k)))
        reranked = check_conflicts(reranked)

        # --- 第 5 步：格式化输出 ---
        lines = []
        for i, r in enumerate(reranked, start=1):
            vector_part = (
                "仅 BM25 命中" if r.similarity is None else f"向量相似度 {r.similarity:.2f}"
            )
            lines.append(
                f"[{i}]（来源: {r.source}，{vector_part}，"
                f"综合得分 {r.final_score:.2f}）\n{r.document}"
            )
        if not lines:
            return NOT_FOUND_MSG

        return (
            "从知识库中检索到以下相关片段：\n\n"
            + "\n\n".join(lines)
            + "\n\n（以上片段与用户问题无关时，请直接告知用户知识库中没有相关内容，不要编造。）"
        )

    except Exception as e:  # noqa: BLE001 检索失败不能拖垮整条 Agent 链路
        logger.warning("知识库检索异常: %s", e, exc_info=True)
        return f"知识库检索失败：{e}。请告知用户暂时无法查询知识库。"


# ---------- 向后兼容导出 ----------
__all__ = ["search", "build_knowledge_base", "get_collection", "chunk_text", "embed_texts"]
