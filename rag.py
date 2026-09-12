"""
RAG 模块（V3 + V7 企业级增强）。

整体流水线：
    knowledge/*.md --加载--> 原始文档 --切分--> chunks
        --embedding-3向量化--> ChromaDB(持久化 ./chroma_db) --检索--> top_k 片段

V7 增强：
- 配置收口到 config.py（类型安全）
- 新增 embedding API 熔断器（circuit breaker）：连续 N 次失败后短路 30s，
  避免把压力打在已经故障的外部服务上
- 结构化日志替代 print()

设计决定（README 的 V3 章节有完整版）：
- 不用 LangChain 的 DocumentLoader / TextSplitter / VectorStore 封装，
  只用 chromadb 原生 API —— 每个环节自己写，每个参数都要能讲清楚为什么。
- Embedding 走 OpenAI 兼容的 POST /embeddings 接口（智谱 embedding-3），
  base_url / key 从 config.py 读取，与 LLM 共用同一套配置机制，换厂商只改配置。
- build_knowledge_base() 幂等：集合里已有数据就直接跳过，可以反复执行。
- search() 永不抛异常：知识库为空、检索无结果、embedding 接口报错，
  都降级为一段明确的文字返回给 Agent，由 Agent 决定怎么向用户解释。

线程模型说明：ChromaDB 查询（SQLite）和 embedding HTTP 请求都是同步阻塞的，
但本模块整体保持"纯同步函数"—— LangGraph 的 ToolNode 对同步工具是通过
线程池执行器（get_executor_for_config）来跑的，所以阻塞发生在工作线程里，
不会卡住 FastAPI 的事件循环（详见 tools.py 里 search_knowledge 的注释）。
"""

import logging
import os
import time
from enum import Enum

import chromadb
import httpx
from dotenv import load_dotenv

from config import settings

load_dotenv()

logger = logging.getLogger(__name__)

# ---------- 路径与配置 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge")
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_NAME = "knowledge_base"

# V7：从 config.py 读取 embedding 配置，启动时类型校验。
EMBEDDING_MODEL_NAME = settings.embedding_model_name
EMBEDDING_BASE_URL = (settings.embedding_base_url or settings.llm_base_url or "").rstrip("/")
EMBEDDING_API_KEY = settings.embedding_api_key or settings.llm_api_key
EMBEDDING_DIMENSIONS = settings.embedding_dimensions

# 切分参数：chunk 太大则一段里混了多个主题，向量语义被稀释；
# 太小则上下文不完整。中文政策类文本一屏在 200-400 字，350 是经验值。
CHUNK_SIZE = 350
# 相邻 chunk 重叠的字符数：防止关键句子正好被切在边界上一分为二，
# 导致任何单个 chunk 都读不懂完整语义。
CHUNK_OVERLAP = 60

# 相似度阈值：低于它的检索结果直接判为"未找到"。
# 向量检索的 top_k 永远会返回结果（哪怕毫不相关），不加阈值的话
# 乱问的问题也会召回一条最高分的垃圾片段，Agent 容易拿去编答案。
# 0.25 是保守值，可按真实 embedding 的分数分布用环境变量调整；设为 0 可关闭。
SIMILARITY_THRESHOLD = float(settings.embedding_similarity_threshold)

# 熔断器配置（V7）
EMBEDDING_FAILURE_THRESHOLD = settings.embedding_failure_threshold
EMBEDDING_RECOVERY_TIMEOUT = settings.embedding_recovery_timeout

NOT_FOUND_MSG = "未找到相关信息"
KB_NOT_BUILT_MSG = "知识库尚未建立，请先运行 python scripts/build_kb.py 建库"

# 进程内缓存 collection 句柄，避免每次检索都重新打开 SQLite。
_collection_cache = None


# ---------- V7：Embedding API 熔断器 ----------
#
# 为什么需要熔断器：
# embedding 接口是整个链路中最脆弱的外部依赖——它既要发 HTTP 请求，又要把
# 结果存入 SQLite。一旦它开始抖动（超时、5xx），盲目重试只会雪上加霜。
# 熔断器在连续失败 N 次后进入 OPEN 状态，短路所有请求直到恢复超时结束；
# 之后自动进入 HALF_OPEN，放一个探测请求通过——成功则闭合，失败则继续开路。
#
# 状态机：CLOSED -> (N次失败) -> OPEN -> (超时) -> HALF_OPEN -> (成功) -> CLOSED
#                                                        -> (失败) -> OPEN


class _CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_circuit = {
    "state": _CircuitState.CLOSED,
    "last_failure_time": 0.0,
    "consecutive_failures": 0,
}


def _check_circuit():
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


def _record_circuit_success():
    """成功时重置熔断器状态。"""
    _circuit["consecutive_failures"] = 0
    _circuit["state"] = _CircuitState.CLOSED


def _record_circuit_failure():
    """失败时累加计数，达到阈值则打开熔断器。"""
    _circuit["consecutive_failures"] += 1
    _circuit["last_failure_time"] = time.time()
    if _circuit["consecutive_failures"] >= EMBEDDING_FAILURE_THRESHOLD:
        _circuit["state"] = _CircuitState.OPEN
        logger.warning(
            f"Embedding API 连续失败 {_circuit['consecutive_failures']} 次，"
            f"触发熔断，{EMBEDDING_RECOVERY_TIMEOUT}s 后恢复"
        )


# ---------- 1. 文档加载 ----------
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
    return docs


# ---------- 2. 切分（chunking）----------
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
        while len(current) > chunk_size:
            chunks.append(current[:chunk_size])
            current = current[chunk_size - overlap :]
    if current:
        chunks.append(current)
    return chunks


# ---------- 3. 向量化（embedding）----------
def embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 OpenAI 兼容的 /embeddings 接口批量向量化。

    V7：加入熔断器保护，避免在 embedding 服务故障时持续压垮它。
    """
    if not EMBEDDING_BASE_URL or not EMBEDDING_API_KEY:
        raise RuntimeError(
            "缺少 embedding 配置：请在 .env 中配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY"
            "（智谱 embedding-3 需要智谱开放平台的 key）"
        )

    _check_circuit()

    url = f"{EMBEDDING_BASE_URL}/embeddings"
    headers = {"Authorization": f"Bearer {EMBEDDING_API_KEY}"}

    all_vectors: list[list[float] | None] = [None] * len(texts)
    # 分批调用：embedding 接口对单次输入条数有限制，千问限制 10 条一批
    batch_size = 10
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json={
                    "model": EMBEDDING_MODEL_NAME,
                    "input": batch,
                    "dimensions": EMBEDDING_DIMENSIONS,
                },
                timeout=60.0,
            )
        except httpx.TimeoutException:
            _record_circuit_failure()
            raise RuntimeError(
                f"Embedding API 超时（batch 起始位置 {start}，model={EMBEDDING_MODEL_NAME}）"
            )
        except httpx.RequestError as e:
            _record_circuit_failure()
            raise RuntimeError(f"Embedding API 请求失败（batch 起始位置 {start}）: {e}")

        if resp.status_code != 200:
            _record_circuit_failure()
            raise RuntimeError(
                f"embedding 接口返回 {resp.status_code}（model={EMBEDDING_MODEL_NAME}，"
                f"base_url={EMBEDDING_BASE_URL}）：{resp.text[:300]}"
            )

        data = resp.json()["data"]  # 返回值带 index 字段，按它排回原顺序
        for item in data:
            all_vectors[start + item["index"]] = item["embedding"]

    _record_circuit_success()
    return all_vectors  # type: ignore[return-value]


# ---------- 4. ChromaDB 集合 ----------
def get_collection():
    """获取（或创建）持久化的 ChromaDB 集合。

    hnsw:space 用 cosine 而不是默认的 l2：cosine 只关心向量方向（语义相似度），
    不关心模长，对文本 embedding 是标准选择。
    """
    global _collection_cache
    if _collection_cache is None:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection_cache = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection_cache


# ---------- 5. 建库（幂等）----------
def build_knowledge_base(rebuild: bool = False) -> dict:
    """加载知识文档 → 切分 → 向量化 → 写入 ChromaDB。

    幂等逻辑：集合里已经有数据且未指定 rebuild 时直接跳过，
    所以重复运行、或者服务启动时误调用它，都不会重复入库。

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
        client = collection._client  # noqa: SLF001 重建时直接删集合最干净
        client.delete_collection(COLLECTION_NAME)
        global _collection_cache
        _collection_cache = None
        collection = get_collection()

    # 所有 chunk 平铺，并记录来源文档，作为 metadata 存进去
    texts: list[str] = []
    metadatas: list[dict] = []
    for doc in docs:
        for i, chunk in enumerate(chunk_text(doc["text"])):
            texts.append(chunk)
            metadatas.append({"source": doc["source"], "chunk_index": i})

    logger.info("加载 %d 篇文档，切分出 %d 个 chunk，开始向量化...", len(docs), len(texts))
    vectors = embed_texts(texts)

    # 批量写入。id 用 文件名::序号，既唯一又便于人工排查。
    collection.add(
        ids=[f"{m['source']}::{i}" for i, m in enumerate(metadatas)],
        embeddings=vectors,
        documents=texts,
        metadatas=metadatas,
    )
    logger.info("建库完成：%d 个 chunk 已写入 %s", collection.count(), CHROMA_DIR)
    return {"files": len(docs), "chunks": len(texts), "skipped": False}


# ---------- 6. 检索 ----------
def search(query: str, top_k: int = 3) -> str:
    """向量检索：query 向量化 → ChromaDB 余弦相似度 → top_k 片段。

    返回给上层的是一段带来源标注的纯文本上下文，Agent 拿到它后
    组织成自然语言回复。注意这个函数永远不抛异常：
    - 知识库没建 / 为空   → 提示先建库
    - 检索结果为空        → "未找到相关信息"
    - embedding 接口挂了  → 明确的失败说明
    这样 Agent 在任何情况下都能把结果继续往下说，而不是整条链路 500。
    """
    try:
        collection = get_collection()
        if collection.count() == 0:
            return KB_NOT_BUILT_MSG

        query_vector = embed_texts([query])[0]
        result = collection.query(
            query_embeddings=[query_vector],
            n_results=min(top_k, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        documents = result["documents"][0]
        if not documents:
            return NOT_FOUND_MSG

        # cosine distance ∈ [0, 2]，相似度 = 1 - distance，越大越相关
        hits = []
        for doc, meta, dist in zip(documents, result["metadatas"][0], result["distances"][0]):
            similarity = 1 - dist
            if similarity < SIMILARITY_THRESHOLD:
                continue  # 低于阈值视为不相关，过滤掉而不是喂给模型
            hits.append((similarity, meta.get("source", "未知来源"), doc))
        if not hits:
            return NOT_FOUND_MSG
        lines = [
            f"[{i}]（来源: {source}，相似度 {similarity:.2f}）\n{doc}"
            for i, (similarity, source, doc) in enumerate(hits, start=1)
        ]
        if not lines:
            return NOT_FOUND_MSG
        return (
            "从知识库中检索到以下相关片段：\n\n"
            + "\n\n".join(lines)
            + "\n\n（如果以上片段与用户问题无关，请直接告知用户知识库中没有相关内容，不要编造。）"
        )
    except Exception as e:  # noqa: BLE001 检索失败不能拖垮整个 Agent 链路
        logger.warning("知识库检索异常: %s", e)
        return f"知识库检索失败：{e}。请告知用户暂时无法查询知识库。"
