"""mcp_server.server 单元测试（mock DB/rag，无需真实 MCP Server）。

覆盖 mcp_server/server.py 中各工具函数的正常和异常路径。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_db():
    with patch("mcp_server.server.db") as m:
        m.ensure_tables = AsyncMock()
        m.Order = MagicMock
        m.Ticket = MagicMock
        m.session_scope = MagicMock()
        yield m


@pytest.fixture
def mock_rag():
    with patch("mcp_server.server.rag") as m:
        m.search.return_value = "相关知识片段：退货政策是7天无理由。"
        yield m


# ──────────────────────────────────────────────
# query_order（MCP 工具）
# ──────────────────────────────────────────────
class TestMcpQueryOrder:
    @pytest.mark.asyncio
    async def test_order_not_found(self, mock_db):
        from mcp_server.server import query_order

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)
        mock_db.session_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_db.session_scope.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await query_order("ORD-999")
        data = json.loads(result)
        assert "error" in data
        assert "ORD-999" in data["error"]

    @pytest.mark.asyncio
    async def test_order_found(self, mock_db):
        from mcp_server.server import query_order

        order = MagicMock()
        order.order_id = "ORD-123"
        order.user_id = "u1"
        order.status = "shipped"
        order.logistics_status = "in_transit"
        order.logistics_detail = "包裹已发出"
        order.created_at = MagicMock()
        order.created_at.isoformat.return_value = "2026-01-15T10:00:00+00:00"

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=order)
        mock_db.session_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_db.session_scope.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await query_order("ORD-123")
        data = json.loads(result)
        assert data["order_id"] == "ORD-123"
        assert data["status"] == "shipped"

    @pytest.mark.asyncio
    async def test_db_error_returns_error_json(self, mock_db):
        from mcp_server.server import query_order

        mock_db.ensure_tables = AsyncMock(side_effect=Exception("db down"))

        result = await query_order("ORD-123")
        data = json.loads(result)
        assert "error" in data
        assert "数据库暂时不可用" in data["error"]


# ──────────────────────────────────────────────
# search_knowledge（MCP 工具）
# ──────────────────────────────────────────────
class TestMcpSearchKnowledge:
    def test_search_with_results(self, mock_rag):
        from mcp_server.server import search_knowledge

        result = search_knowledge("退货政策")
        assert "退货" in result
        mock_rag.search.assert_called_once_with("退货政策")

    def test_search_no_result(self, mock_rag):
        from mcp_server.server import search_knowledge

        mock_rag.search.return_value = "未找到相关知识。"
        result = search_knowledge("xxx")
        assert "未找到" in result

    def test_search_exception(self, mock_rag):
        from mcp_server.server import search_knowledge

        mock_rag.search.side_effect = Exception("connection lost")
        result = search_knowledge("query")
        assert "知识库检索失败" in result


# ──────────────────────────────────────────────
# create_ticket（MCP 工具）
# ──────────────────────────────────────────────
class TestMcpCreateTicket:
    @pytest.mark.asyncio
    async def test_order_not_found(self, mock_db):
        from mcp_server.server import create_ticket

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)
        mock_db.session_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_db.session_scope.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await create_ticket("ORD-999", "物流异常")
        data = json.loads(result)
        assert "error" in data
        assert "ORD-999" in data["error"]

    @pytest.mark.asyncio
    async def test_ticket_created(self, mock_db):
        from mcp_server.server import create_ticket

        order = MagicMock()

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=order)
        # add is synchronous, flush/commit are async
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()

        # Create a proper async context manager
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_db.session_scope.return_value = mock_session_ctx

        ticket = MagicMock()
        ticket.ticket_id = 1
        ticket.order_id = "ORD-123"
        ticket.reason = "物流异常"
        ticket.status = "待处理"
        mock_db.Ticket = MagicMock(return_value=ticket)

        result = await create_ticket("ORD-123", "物流异常")
        data = json.loads(result)
        assert data["ticket_id"] == 1
        assert data["status"] == "待处理"

    @pytest.mark.asyncio
    async def test_db_error_returns_error_json(self, mock_db):
        from mcp_server.server import create_ticket

        mock_db.ensure_tables = AsyncMock(side_effect=Exception("boom"))

        result = await create_ticket("ORD-123", "原因")
        data = json.loads(result)
        assert "error" in data
        assert "数据库暂时不可用" in data["error"]
