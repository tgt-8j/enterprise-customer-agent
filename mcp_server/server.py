"""MCP Server：将客服工具的三个核心能力通过 MCP 协议暴露。

这个 server 进程可以独立运行（默认端口 8001），
Agent 通过 langchain-mcp-adapters 连接并消费工具。

工具列表：
- query_order:       查询订单状态和物流信息
- search_knowledge:  检索知识库中的处理规则
- create_ticket:     创建人工工单

启动方式：
    python -m mcp_server              # HTTP 传输，监听 MCP_HOST:MCP_PORT（默认 0.0.0.0:8001）
    python -m mcp_server --stdio      # stdio 传输，供本地进程调用
"""

from fastmcp import FastMCP

import db
import rag
from metrics import AGENT_TOOL_CALLS_TOTAL
from tools import _dumps

mcp = FastMCP("CustomerServiceTools")


@mcp.tool()
async def query_order(order_id: str) -> str:
    """根据订单号查询订单状态和物流信息。输入订单号字符串，返回订单详情 JSON。"""
    try:
        await db.ensure_tables()
        async with db.session_scope() as session:
            order = await session.get(db.Order, order_id)
        if order is None:
            AGENT_TOOL_CALLS_TOTAL.labels(tool_name="query_order", result="not_found").inc()
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
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="query_order", result="success").inc()
        return result
    except Exception as e:
        return _dumps({"error": f"数据库暂时不可用：{e}"})


@mcp.tool()
def search_knowledge(query: str) -> str:
    """在企业知识库中检索与用户问题相关的处理规则或FAQ。输入用户问题，返回相关知识片段及其来源。"""
    try:
        result = rag.search(query)
        has_hits = "未找到" not in result and "尚未建立" not in result and "失败" not in result
        AGENT_TOOL_CALLS_TOTAL.labels(
            tool_name="search_knowledge", result="success" if has_hits else "no_result"
        ).inc()
        return result
    except Exception as e:
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="search_knowledge", result="error").inc()
        return f"知识库检索失败：{e}。请告知用户暂时无法查询知识库。"


@mcp.tool()
async def create_ticket(order_id: str, reason: str) -> str:
    """当判断需要人工介入时创建工单。输入订单号和创建原因，返回生成的工单号 JSON。"""
    try:
        await db.ensure_tables()
        async with db.session_scope() as session:
            order = await session.get(db.Order, order_id)
            if order is None:
                AGENT_TOOL_CALLS_TOTAL.labels(
                    tool_name="create_ticket", result="order_not_found"
                ).inc()
                return _dumps({"error": f"订单 {order_id} 不存在，无法创建工单"})
            ticket = db.Ticket(order_id=order_id, reason=reason, status="待处理")
            session.add(ticket)
            await session.flush()
            result = {
                "ticket_id": ticket.ticket_id,
                "order_id": ticket.order_id,
                "reason": ticket.reason,
                "status": ticket.status,
            }
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="create_ticket", result="success").inc()
        return _dumps(result)
    except Exception as e:
        AGENT_TOOL_CALLS_TOTAL.labels(tool_name="create_ticket", result="error").inc()
        return _dumps({"error": f"数据库暂时不可用，工单未创建：{e}"})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MCP Server for Customer Service Tools")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind to")
    parser.add_argument(
        "--transport",
        choices=["http", "sse", "streamable-http"],
        default="streamable-http",
        help="Transport type",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport, host=args.host, port=args.port)
