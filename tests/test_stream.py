"""流式 Agent 测试：验证 astream_events 事件格式。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from agent import build_agent


class FakeChunk:
    """模拟 LLM 流式输出的 chunk。"""

    def __init__(self, content: str):
        self.content = content


class FakeAgent:
    """模拟 Agent，用于测试流式事件。"""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, input_data, version="v2"):
        """返回异步生成器。"""
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_stream_events_token():
    """测试 token 流式事件。"""
    events = [
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk("你好")},
        },
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk("，世界")},
        },
    ]

    fake_agent = FakeAgent(events)
    collected = []
    async for event in fake_agent.astream_events(
        {"messages": [HumanMessage("你好")]}, version="v2"
    ):
        collected.append(event)

    assert len(collected) == 2
    assert collected[0]["event"] == "on_chat_model_stream"
    assert collected[1]["data"]["chunk"].content == "，世界"


@pytest.mark.asyncio
async def test_stream_events_with_tool_call():
    """测试工具调用流式事件。"""
    events = [
        {"event": "on_tool_start", "name": "query_order", "data": {"input": {"order_id": "123"}}},
        {"event": "on_tool_end", "name": "query_order", "data": {"output": '{"status":"shipped"}'}},
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk("您的订单正在运输中")},
        },
    ]

    fake_agent = FakeAgent(events)
    collected = []
    async for event in fake_agent.astream_events(
        {"messages": [HumanMessage("查订单")]}, version="v2"
    ):
        collected.append(event)

    assert any(e["event"] == "on_tool_start" for e in collected)
    assert any(e["event"] == "on_tool_end" for e in collected)


@pytest.mark.asyncio
async def test_build_agent_with_intent():
    """测试带意图参数的 agent 构建。"""
    agent = build_agent(intent="chitchat")
    assert agent is not None


@pytest.mark.asyncio
async def test_get_tools_for_intent():
    """测试工具路由。"""
    from agent import get_tools_for_intent

    tools = await get_tools_for_intent("query_order")
    assert len(tools) == 1
    assert tools[0].name == "query_order"

    tools = await get_tools_for_intent("chitchat")
    assert len(tools) == 0

    tools = await get_tools_for_intent("general")
    assert len(tools) == 3


def test_get_tools_for_intent_knowledge_search():
    """测试 knowledge_search 意图只返回 search_knowledge 工具。"""
    from agent import _get_tools_for_intent

    tools = _get_tools_for_intent("knowledge_search")
    assert len(tools) == 1
    assert tools[0].name == "search_knowledge"


def test_get_tools_for_intent_mcp():
    """测试 use_mcp=True 时返回所有工具。"""
    from agent import _get_tools_for_intent

    tools = _get_tools_for_intent("general", use_mcp=True)
    assert len(tools) == 3


@pytest.mark.asyncio
async def test_get_mcp_tools_success():
    """测试 MCP 加载成功路径（mock MCP client）。"""
    from agent import get_mcp_tools

    mock_tool = MagicMock()
    mock_tool.name = "query_order"
    mock_client = AsyncMock()
    mock_client.get_tools = AsyncMock(return_value=[mock_tool])

    # 因为 MultiServerMCPClient 是在 agent.py 内部 import 的，
    # 所以 patch 源头模块
    with patch("langchain_mcp_adapters.client.MultiServerMCPClient") as mock_mcp_client:
        mock_mcp_client.return_value = mock_client
        tools = await get_mcp_tools()
        assert len(tools) == 1
        assert tools[0].name == "query_order"


@pytest.mark.asyncio
async def test_get_mcp_tools_fallback():
    """测试 MCP 加载失败时回退到本地工具。"""
    from agent import get_mcp_tools
    from tools import ALL_TOOLS

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        side_effect=Exception("Connection failed"),
    ):
        tools = await get_mcp_tools()
        assert len(tools) == len(ALL_TOOLS)
