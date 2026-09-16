"""意图分发：根据用户输入判断意图并返回结构化决策。

意图分类：
- query_order:    查询订单状态、物流信息
- knowledge_search: 咨询政策、规则、时效
- create_ticket:  需要人工介入、建工单
- chitchat:       闲聊、问候、其他

使用方式：
    from dispatch import classify_intent
    decision = await classify_intent.ainvoke("查一下订单9527的物流")
"""

import re
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class DispatchDecision(BaseModel):
    """意图分类结果"""

    intent: Literal["query_order", "knowledge_search", "create_ticket", "chitchat"] = Field(
        description="识别出的意图类型"
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="分类置信度 0~1")
    reasoning: str = Field(default="", description="分类理由")
    extracted_order_id: str | None = Field(
        default=None, description="从文本中提取的订单号（如果有）"
    )


# 关键词路由表（用于快速分类，避免每次调 LLM）
_INTENT_RULES: list[
    tuple[Literal["query_order", "knowledge_search", "create_ticket", "chitchat"], str, float]
] = [
    (
        "query_order",
        r"查.*订单|订单.*查|物流.*哪|快递.*到哪|物流.*状态|我的快递|包裹|发货|配送",
        0.85,
    ),
    (
        "knowledge_search",
        r"退货|退款|换货|发票|开票|7天|48小时|无理由|质量问题|运费|价保|签收|验货|政策|规则|时效",
        0.85,
    ),
    (
        "create_ticket",
        r"建.*单|工单|转人工|投诉|联系人工|帮我处理|出问题|异常.*处理",
        0.80,
    ),
]

# 订单号提取正则：支持 6-20 位数字（不使用 \b，因为中文字符不是 word boundary）
_ORDER_ID_RE = re.compile(r"(\d{6,20})")


def _extract_order_id(text: str) -> str | None:
    """从用户输入中提取订单号。"""
    match = _ORDER_ID_RE.search(text)
    return match.group(1) if match else None


def _classify_by_keywords(
    query: str,
) -> tuple[Literal["query_order", "knowledge_search", "create_ticket", "chitchat"], float, str]:
    """基于关键词规则快速分类。返回 (intent, confidence, reasoning)。"""
    query_lower = query.lower()
    for intent, pattern, confidence in _INTENT_RULES:
        if re.search(pattern, query_lower):
            return intent, confidence, f"关键词匹配: {pattern}"
    return "chitchat", 0.5, "无匹配关键词，降级为闲聊"


async def classify_intent(query: str) -> DispatchDecision:
    """
    分析用户查询并返回意图分类决策。

    策略：先用关键词规则快速分类，命中则直接返回；
    未命中或边界情况可升级为 LLM 判断（预留扩展点）。
    """
    intent, confidence, reasoning = _classify_by_keywords(query)
    order_id = _extract_order_id(query) if intent == "query_order" else None
    return DispatchDecision(
        intent=intent,
        confidence=confidence,
        reasoning=reasoning,
        extracted_order_id=order_id,
    )


# 供 LangGraph 使用的 tool 接口
classify_intent_tool = tool(classify_intent)
