"""工具层单元测试。

测试策略：
- 直接调用 @tool 装饰后的函数（通过 ainvoke），mock 底层 db / rag 依赖
- 覆盖成功、未找到、异常三种分支
- 同时保留对 db.py 和 rag.py 的间接验证
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db
import rag
from tools import query_order, search_knowledge, create_ticket, ALL_TOOLS


class TestQueryOrder:
    """直接调用 query_order 工具函数。"""

    @pytest.mark.asyncio
    async def test_success(self):
        """订单查询成功路径。"""
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

                result = await query_order.ainvoke({"order_id": "12345"})
                assert isinstance(result, str)
                data = json.loads(result)
                assert data["order_id"] == "12345"
                assert data["status"] == "已发货"

    @pytest.mark.asyncio
    async def test_not_found(self):
        """订单不存在时返回结构化错误。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=None)
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await query_order.ainvoke({"order_id": "99999"})
                assert isinstance(result, str)
                data = json.loads(result)
                assert "error" in data
                assert "不存在" in data["error"]

    @pytest.mark.asyncio
    async def test_db_error_fallback(self):
        """数据库异常时返回友好错误，不抛异常。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(side_effect=Exception("连接超时"))
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await query_order.ainvoke({"order_id": "88888"})
                assert isinstance(result, str)
                data = json.loads(result)
                assert "error" in data
                assert "数据库暂时不可用" in data["error"]


class TestSearchKnowledge:
    """直接调用 search_knowledge 工具函数。"""

    def test_success(self):
        """检索成功返回相关内容。"""
        with patch.object(rag, "search", return_value="退货政策：7天内可退，质量问题可换货"):
            result = search_knowledge.invoke({"query": "退货政策"})
            assert "7天" in result
            assert "质量问题" in result

    def test_no_result(self):
        """无匹配时返回空提示。"""
        with patch.object(rag, "search", return_value="未找到相关信息"):
            result = search_knowledge.invoke({"query": "太阳系行星数量"})
            assert "未找到" in result

    def test_rag_error_fallback(self):
        """RAG 异常时返回友好错误。"""
        with patch.object(rag, "search", side_effect=Exception("ChromaDB 连接失败")):
            result = search_knowledge.invoke({"query": "运费规则"})
            assert isinstance(result, str)
            assert "知识库检索失败" in result


class TestCreateTicket:
    """直接调用 create_ticket 工具函数。"""

    @pytest.mark.asyncio
    async def test_success(self):
        """工单创建成功。"""
        mock_order = MagicMock()
        mock_order.order_id = "12345"

        mock_ticket = MagicMock()
        mock_ticket.ticket_id = 1001
        mock_ticket.order_id = "12345"
        mock_ticket.reason = "物流异常"
        mock_ticket.status = "待处理"

        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=mock_order)
                mock_session.add = MagicMock()
                mock_session.flush = AsyncMock()
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
                # db.Ticket 在函数内部实例化，需要 mock 它的 ticket_id
                mock_ticket_obj = MagicMock()
                mock_ticket_obj.ticket_id = 1001
                mock_ticket_obj.order_id = "12345"
                mock_ticket_obj.reason = "物流异常"
                mock_ticket_obj.status = "待处理"
                with patch.object(db, "Ticket", return_value=mock_ticket_obj):
                    result = await create_ticket.ainvoke({"order_id": "12345", "reason": "物流异常"})
                assert isinstance(result, str)
                data = json.loads(result)
                assert data["ticket_id"] == 1001
                assert data["status"] == "待处理"

    @pytest.mark.asyncio
    async def test_order_not_found(self):
        """订单不存在时拒绝建单。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(return_value=None)
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await create_ticket.ainvoke({"order_id": "99999", "reason": "测试"})
                data = json.loads(result)
                assert "error" in data
                assert "不存在" in data["error"]

    @pytest.mark.asyncio
    async def test_db_error_fallback(self):
        """数据库异常时返回友好错误。"""
        with patch.object(db, "ensure_tables", new_callable=AsyncMock):
            with patch.object(db, "session_scope") as mock_scope:
                mock_session = AsyncMock()
                mock_session.get = AsyncMock(side_effect=Exception("数据库宕机"))
                mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await create_ticket.ainvoke({"order_id": "77777", "reason": "测试"})
                data = json.loads(result)
                assert "error" in data
                assert "数据库暂时不可用" in data["error"]


class TestAllTools:
    """工具集合完整性校验。"""

    def test_all_tools_list(self):
        assert len(ALL_TOOLS) == 3
        names = {t.name for t in ALL_TOOLS}
        assert names == {"query_order", "search_knowledge", "create_ticket"}

    def test_all_tools_are_structured(self):
        for t in ALL_TOOLS:
            assert hasattr(t, "name")
            assert hasattr(t, "description")
