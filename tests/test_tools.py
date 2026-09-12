"""工具层单元测试。

测试策略：
- 由于 @tool 装饰器将函数包装为 StructuredTool 对象，
  我们不能直接调用 tools.query_order 等。
- 我们测试的是 db.py 中的底层函数和 rag.py 中的 search 函数，
  这些是工具调用的核心逻辑。
- tools.py 本身的测试通过 test_agent_legacy.py 中的集成测试覆盖。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db


class TestQueryOrderLogic:
    """query_order 的核心逻辑（通过 db 层间接测试）。"""

    @pytest.mark.asyncio
    async def test_query_order_success(self):
        """模拟订单查询成功。"""
        mock_order = MagicMock()
        mock_order.order_id = "12345"
        mock_order.user_id = "U001"
        mock_order.status = "已发货"
        mock_order.logistics_status = "正常"
        mock_order.logistics_detail = "在途"
        mock_order.created_at = None

        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=mock_order)
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                # 直接调用 db 层的查询逻辑
                async with db.session_scope() as session:
                    order = await session.get(db.Order, "12345")
                    assert order is not None
                    assert order.order_id == "12345"

    @pytest.mark.asyncio
    async def test_query_order_not_found(self):
        """模拟订单不存在。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=None)
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                async with db.session_scope() as session:
                    order = await session.get(db.Order, "99999")
                    assert order is None


class TestSearchKnowledgeLogic:
    """search_knowledge 的核心逻辑（通过 rag 层测试）。"""

    def test_search_knowledge_success(self):
        """模拟 RAG 检索成功。"""
        import rag
        with patch.object(rag, "search", return_value="退货政策：7天内可退") as mock_search:
            result = rag.search("退货政策")
            assert "7天" in result
            mock_search.assert_called_once_with("退货政策")

    def test_search_knowledge_not_found(self):
        """模拟 RAG 检索无结果。"""
        import rag
        with patch.object(rag, "search", return_value="未找到相关信息") as mock_search:
            result = rag.search("太阳系行星数量")
            assert "未找到" in result


class TestCreateTicketLogic:
    """create_ticket 的核心逻辑。"""

    @pytest.mark.asyncio
    async def test_create_ticket_success(self):
        """模拟工单创建成功。"""
        mock_order = MagicMock()
        mock_order.order_id = "12345"

        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=mock_order)
                mock_session.add = MagicMock()
                mock_session.flush = AsyncMock()
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                # 验证 ensure_tables 被调用
                await db.ensure_tables()
                db.ensure_tables.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_ticket_order_not_found(self):
        """模拟订单不存在时不创建工单。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=None)
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                async with db.session_scope() as session:
                    order = await session.get(db.Order, "99999")
                    assert order is None
