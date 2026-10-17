"""两级重排序模块。

检索链路中的 rerank 阶段负责在召回后进一步精排：
  第一级（粗筛）：Embedding 余弦相似度筛选 Top-K粗
  第二级（精排）：按来源权重 + 位置衰减重新打分

设计理念：
- 第一级过滤掉明显不相关的文档，减少第二级计算量
- 第二级引入来源可信度（.gov.cn > 新闻 > 博客），让 Agent 优先引用权威来源
- 降级策略：Embedding 服务不可用时，按来源权重退化为规则排序
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 来源可信度权重（越高越权威）
SOURCE_WEIGHTS: dict[str, float] = {
    ".gov.cn": 0.95,
    ".edu.cn": 0.90,
    "official": 0.88,
    "news": 0.75,
    "blog": 0.60,
    "forum": 0.50,
}
DEFAULT_WEIGHT = 0.55

# 位置衰减系数：排名越靠后，得分越低
POSITION_DECAY = 0.15


@dataclass
class RerankResult:
    """Rerank 后的单条结果。

    similarity 为 None 表示该文档只被 BM25 召回、没有向量分数。
    """

    document: str
    source: str
    original_rank: int
    similarity: float | None
    final_score: float
    disputed: bool = False


def _get_source_label(doc_source: str) -> str:
    """从来源路径提取标签（用于权重匹配）。"""
    src_lower = doc_source.lower()
    for key in SOURCE_WEIGHTS:
        if key in src_lower:
            return key
    return "other"


def _source_weight(source: str) -> float:
    """获取来源可信度权重。"""
    label = _get_source_label(source)
    return SOURCE_WEIGHTS.get(label, DEFAULT_WEIGHT)


def two_stage_rerank(
    candidates: list[dict[str, Any]],
    embedding_similarities: list[float | None] | None = None,
    top_k_final: int = 5,
) -> list[RerankResult]:
    """两级 Rerank：粗筛 + 精排。

    Args:
        candidates: 召回的文档列表，每项含 'document', 'source'
        embedding_similarities: 对应的向量相似度；None 表示该候选没有向量分
                                （只被 BM25 召回），不会被粗筛丢弃。
                                整个参数为 None 时退化为纯来源权重排序
        top_k_final: 最终返回条数

    Returns:
        精排后的 RerankResult 列表，按 final_score 降序排列
    """
    if not candidates:
        return []

    # --- 第一级：粗筛（基于 embedding 相似度）---
    if embedding_similarities is not None and len(embedding_similarities) == len(candidates):
        # 只丢「有向量分但分数偏低」的候选；没有向量分的（BM25 独有）必须保留。
        # 此前统一用 `similarity > 0.3` 过滤，把 BM25 独有候选（相似度记 0.0）
        # 全部丢掉，导致 embedding 故障时「纯 BM25 兜底」实际返回空结果。
        filtered = []
        for idx, candidate in enumerate(candidates):
            sim = embedding_similarities[idx]
            if sim is None or sim > 0.3:
                filtered.append((idx, candidate))
    else:
        # 退化：无 embedding 数据，全部参与精排
        filtered = list(enumerate(candidates))

    if not filtered:
        return []

    # --- 第二级：精排（来源权重 + 位置衰减）---
    results = []
    for rank, (orig_idx, candidate) in enumerate(filtered):
        sim = embedding_similarities[orig_idx] if embedding_similarities is not None else None
        source = candidate.get("source", "未知来源")
        doc = candidate.get("document", "")
        weight = _source_weight(source)
        # 位置衰减：越靠后的结果扣分越多
        position_penalty = rank * POSITION_DECAY
        # 没有向量分时 sim 按 0 计入，靠来源权重与位置衰减排序
        final_score = (sim or 0.0) * 0.5 + weight * 0.5 - position_penalty
        results.append(
            RerankResult(
                document=doc,
                source=source,
                original_rank=orig_idx,
                similarity=sim,
                final_score=final_score,
            )
        )

    # 按 final_score 降序排列
    results.sort(key=lambda x: x.final_score, reverse=True)
    return results[:top_k_final]


# 矛盾关键词对：同一主题出现正反两面时标记冲突
_CONTRASTING_KEYWORDS: list[tuple[str, str]] = [
    ("支持", "反对"),
    ("通过", "未通过"),
    ("批准", "驳回"),
    ("有效", "无效"),
    ("确认", "否认"),
    ("合格", "不合格"),
    ("可以", "不可以"),
    ("应当", "不应当"),
]


def _has_contrasting_content(doc_a: str, doc_b: str) -> bool:
    """判断两段文档是否存在矛盾表述。"""
    a_lower = doc_a.lower()
    b_lower = doc_b.lower()
    for pos_kw, neg_kw in _CONTRASTING_KEYWORDS:
        pos_in_a = pos_kw in a_lower
        pos_in_b = pos_kw in b_lower
        neg_in_a = neg_kw in a_lower
        neg_in_b = neg_kw in b_lower
        # 一段含正面词、另一段含对应负面词 => 矛盾
        if (pos_in_a and neg_in_b) or (pos_in_b and neg_in_a):
            return True
    return False


def check_conflicts(results: list[RerankResult]) -> list[RerankResult]:
    """冲突检测：同一主题但观点矛盾的文档标记 disputed=True。

    规则：对 results 中任意两个文档，若 _has_contrasting_content 判定为矛盾，
    则同时标记 disputed=True，降低其 final_score（乘以 0.7 惩罚系数），
    让 Agent 在引用时能识别并优先呈现非冲突来源。

    后续可替换为 LLM 裁决（传入两段文档 + 原始 query，由模型判断是否矛盾）。
    """
    if len(results) < 2:
        return results

    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            if _has_contrasting_content(results[i].document, results[j].document):
                results[i].disputed = True
                results[j].disputed = True
                logger.info(
                    "检测到矛盾文档: %s vs %s",
                    results[i].source,
                    results[j].source,
                )

    # 冲突文档惩罚分数，使其排序下降
    for r in results:
        if r.disputed:
            r.final_score *= 0.7

    results.sort(key=lambda x: x.final_score, reverse=True)
    return results
