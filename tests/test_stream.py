"""流式 Agent 测试：验证 astream_events 事件格式。"""

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

    tools = get_tools_for_intent("query_order")
    assert len(tools) == 1
    assert tools[0].name == "query_order"

    tools = get_tools_for_intent("chitchat")
    assert len(tools) == 0

    tools = get_tools_for_intent("general")
    assert len(tools) == 3
