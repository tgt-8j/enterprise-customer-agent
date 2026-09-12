"""
不需要真实 API Key 也能验证这个 Agent 的图结构对不对。

思路：用一个"假LLM"，让它按脚本依次返回：
    第1次调用 -> 决定调用 query_order
    第2次调用 -> 看到订单异常，决定调用 search_knowledge
    第3次调用 -> 决定调用 create_ticket
    第4次调用 -> 给出最终自然语言回复（不再调用任何工具）

如果整个 StateGraph 搭对了，跑完这4轮之后应该正确停在 END，
并且 main.py 里的轨迹组装逻辑能正确把这3次工具调用还原出来。

这不是在测试"LLM聪不聪明"，而是在测试"图的状态流转对不对"——
这才是面试会问到的部分，也是真正接入真实模型之前最值得先跑通的东西。

V7：保留原有测试逻辑，适配新的 config 模块。
"""
import asyncio

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agent import build_agent


class FakeToolCallingLLM(FakeMessagesListChatModel):
    """
    真实的 ChatOpenAI 会把 tools 的 schema 发给模型，让模型自己决定调不调、调哪个。
    这里我们不需要真的做这个决策过程（脚本已经写死了顺序），
    只需要让 .bind_tools() 这个接口存在、不报错即可，所以直接返回自身。
    """

    def bind_tools(self, tools, **kwargs):
        return self


def make_scripted_llm():
    scripted_responses = [
        AIMessage(
            content="",
            tool_calls=[{"name": "query_order", "args": {"order_id": "9527"}, "id": "call_1"}],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "args": {"query": "物流长时间不更新"}, "id": "call_2"}],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "create_ticket", "args": {"order_id": "9527", "reason": "物流异常超48小时未更新"}, "id": "call_3"}],
        ),
        AIMessage(
            content="已为您查询订单9527，物流在广州转运中心滞留超48小时，判定为物流异常，已为您创建工单 TK-XXXXXXXX，客服会尽快跟进处理。",
        ),
    ]
    return FakeToolCallingLLM(responses=scripted_responses)


def run_async(coro):
    """在独立事件循环里跑一个协程，结束后丢弃连接池。

    asyncpg 连接绑定在创建它的循环上，而 asyncio.run() 每次都开新循环；
    生产环境 uvicorn 只有一个常驻循环不受影响，但测试进程里连续多个
    asyncio.run 会让连接池里的旧连接失效（Event loop is closed）。
    所以每次循环结束后显式 dispose，下个循环重建连接。
    """
    import db

    try:
        return asyncio.run(coro)
    finally:
        asyncio.run(db.engine.dispose())


def test_full_loop():
    fake_llm = make_scripted_llm()
    agent = build_agent(llm=fake_llm)

    # V5：工具改成 async（真查/真写 PostgreSQL）后，整条链路走 ainvoke，
    # 和 main.py 的生产路径保持一致（同步 invoke 已无法执行 async 工具）。
    result = run_async(
        agent.ainvoke(
            {"messages": [HumanMessage(content="我的订单9527为什么还没发货，如果物流异常帮我创建工单")]}
        )
    )

    messages = result["messages"]

    # 断言1：最后一条消息是没有 tool_calls 的最终回复
    final = messages[-1]
    assert isinstance(final, AIMessage)
    assert not final.tool_calls
    assert "工单" in final.content
    print("✅ 最终回复正确生成:", final.content)

    # 断言2：三个工具确实都被依次调用了
    from langchain_core.messages import ToolMessage
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 3
    print(f"✅ 共执行了 {len(tool_messages)} 次工具调用，依次为:")
    for tm in tool_messages:
        print(f"   - {tm.name}: {tm.content[:80]}")

    print("\n🎉 最小闭环跑通：agent → tools → agent → tools → agent → tools → agent(结束)")


def test_history_to_messages():
    """历史行 → LangChain 多轮消息数组的角色映射正确。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from main import history_to_messages

    class Row:
        def __init__(self, role, content):
            self.role, self.content = role, content

    msgs = history_to_messages(
        [Row("user", "查订单12345"), Row("assistant", "好的"), Row("user", "有异常吗")]
    )
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage, HumanMessage]
    assert msgs[0].content == "查订单12345"
    print("✅ 历史消息正确映射为多轮 messages 数组（user→Human, assistant→AI）")


if __name__ == "__main__":
    test_full_loop()
    test_history_to_messages()
