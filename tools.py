"""
三个工具：query_order / search_knowledge / create_ticket。

V5 起状态：三个工具全部接真数据 —— search_knowledge 走 RAG 向量检索（rag.py），
query_order / create_ticket 走 PostgreSQL（db.py，SQLAlchemy 异步 + asyncpg）。

V7 增强：
- 结构化日志：每个工具调用都记录开始/结束/错误，方便排查问题
- Prometheus 指标：工具调用次数和结果分桶统计

异步说明：LangGraph 的 ToolNode 有两条执行路径——同步图 .invoke 时把同步工具
丢进线程池，异步图 .ainvoke 时直接 await 异步工具。V5 起主链路走 ainvoke
（见 main.py），所以 DB 工具是 async 的，asyncpg 连接全程在同一个事件循环上，
DB 阻塞不会卡住 FastAPI。search_knowledge 保持同步（rag.py 是同步实现），
ToolNode 在异步路径下会自动把它放进线程池执行，同样不阻塞事件循环。
"""

import json
import logging
import time

from langchain_core.tools import tool

import db
import rag
from metrics import AGENT_TOOL_CALLS_TOTAL

logger = logging.getLogger(__name__)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@tool
async def query_order(order_id: str) -> str:
    """根据订单号查询订单状态和物流信息。输入订单号字符串，返回订单详情。"""
    start = time.monotonic()
    logger.info("工具调用开始: query_order", extra={"order_id": order_id})

    try:
        await db.ensure_tables()
        async with db.session_scope() as session:
            order = await session.get(db.Order, order_id)
        if order is None:
            # 查不到订单是正常业务分支，返回结构化错误让模型组织话术，不抛异常
            elapsed_ms = (time.monotonic() - start) * 1000
            AGENT_TOOL_CALLS_TOTAL.labels(tool_name="query_order", result="not_found").inc()
            logger.info(
                "工具调用结束: query_order（订单不存在）",
                extra={"order_id": order_id, "elapsed_ms": round(elapsed_ms, 1)},
            )
            return _dumps({"error": f"订单 {order_id} 不存在，请确认订单号是否正确"})

        result = _dumps(
            {
                "order_id": order.order_id,
                "user_id": order.user_id,
                "status": order.status,
                "logistics_status": order.logistics_status,
                "logistics_detail": order.logistics_detail,
                "created_at": order.created_at.isoformat() if order.created_at else None,
            }
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="query_order", result="success").inc()
        logger.info(
            "工具调用结束: query_order（成功）",
            extra={
                "order_id": order_id,
                "status": order.status,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )
        return result
    except Exception as e:  # 连不上库 / 建表失败等：服务不崩，给模型一个明确错误
        elapsed_ms = (time.monotonic() - start) * 1000
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="query_order", result="error").inc()
        logger.warning(
            "工具调用异常: query_order",
            extra={"order_id": order_id, "error": str(e), "elapsed_ms": round(elapsed_ms, 1)},
        )
        return _dumps({"error": f"数据库暂时不可用：{e}"})


@tool
def search_knowledge(query: str) -> str:
    """在企业知识库中检索与用户问题相关的处理规则或FAQ。输入用户问题，返回相关知识片段及其来源。"""
    # V3：真实 RAG。函数签名与 V0 完全一致（输入query，输出相关片段字符串），
    # 内部从"关键词假匹配"换成了 Embedding + ChromaDB 向量检索（见 rag.py），
    # 上层 Agent 图结构零改动——这就是当初面向接口设计换来的好处。
    #
    # 关于异步安全：rag.search 是同步阻塞的（ChromaDB 查询 + embedding HTTP 请求），
    # ToolNode 在 ainvoke 路径下会把这个同步工具放进线程池执行，
    # 阻塞发生在工作线程里，不会卡住 FastAPI 的事件循环。
    start = time.monotonic()
    logger.info("工具调用开始: search_knowledge", extra={"query": query[:100]})

    try:
        result = rag.search(query)
        elapsed_ms = (time.monotonic() - start) * 1000
        has_hits = "未找到" not in result and "尚未建立" not in result and "失败" not in result
        AGENT_TOOL_CALLS_TOTAL.labels(
            tool_name="search_knowledge", result="success" if has_hits else "no_result"
        ).inc()
        logger.info(
            "工具调用结束: search_knowledge",
            extra={"query": query[:100], "elapsed_ms": round(elapsed_ms, 1)},
        )
        return result
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="search_knowledge", result="error").inc()
        logger.warning(
            "工具调用异常: search_knowledge",
            extra={"query": query[:100], "error": str(e), "elapsed_ms": round(elapsed_ms, 1)},
        )
        return f"知识库检索失败：{e}。请告知用户暂时无法查询知识库。"


@tool
async def create_ticket(order_id: str, reason: str) -> str:
    """当判断需要人工介入时创建工单。输入订单号和创建原因，返回生成的工单号。"""
    start = time.monotonic()
    logger.info(
        "工具调用开始: create_ticket",
        extra={"order_id": order_id, "reason": reason[:80]},
    )

    try:
        await db.ensure_tables()
        async with db.session_scope() as session:
            # 先确认订单存在，给模型一个可读的错误，而不是等外键约束炸出 IntegrityError
            order = await session.get(db.Order, order_id)
            if order is None:
                elapsed_ms = (time.monotonic() - start) * 1000
                AGENT_TOOL_CALLS_TOTAL.labels(
                    tool_name="create_ticket", result="order_not_found"
                ).inc()
                logger.info(
                    "工具调用结束: create_ticket（订单不存在）",
                    extra={"order_id": order_id, "elapsed_ms": round(elapsed_ms, 1)},
                )
                return _dumps({"error": f"订单 {order_id} 不存在，无法创建工单"})
            ticket = db.Ticket(order_id=order_id, reason=reason, status="待处理")
            session.add(ticket)
            await session.flush()  # 拿到自增主键 ticket_id
            result = {
                "ticket_id": ticket.ticket_id,
                "order_id": ticket.order_id,
                "reason": ticket.reason,
                "status": ticket.status,
            }
        elapsed_ms = (time.monotonic() - start) * 1000
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="create_ticket", result="success").inc()
        logger.info(
            "工具调用结束: create_ticket（工单 %d）",
            extra={"ticket_id": ticket.ticket_id, "elapsed_ms": round(elapsed_ms, 1)},
        )
        return _dumps(result)
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="create_ticket", result="error").inc()
        logger.warning(
            "工具调用异常: create_ticket",
            extra={"order_id": order_id, "error": str(e), "elapsed_ms": round(elapsed_ms, 1)},
        )
        return _dumps({"error": f"数据库暂时不可用，工单未创建：{e}"})


ALL_TOOLS = [query_order, search_knowledge, create_ticket]
