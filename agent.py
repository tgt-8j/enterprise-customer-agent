"""
手写 StateGraph，而不是直接调用 langgraph.prebuilt 里的 create_react_agent。

为什么要手写：
create_react_agent 一行代码就能给你一个能用的 Agent，但面试官问"这个循环
具体是怎么转的、state 里存了什么、什么时候会停下来"，如果你只会说
"我调了 create_react_agent"，这就立刻暴露你没有理解底层机制。

这里手写的逻辑其实就是 ReAct 循环本身：
    用户输入 → 模型思考(可能决定调工具) → 执行工具 → 把工具结果喂回给模型
    → 模型继续思考(可能再调工具，也可能直接给最终答案) → ... → 直到模型
    不再调用任何工具为止，视为得到最终答案。

状态流转图：
    START → agent 节点 →(有没有 tool_calls?)
                ├─ 有 → tools 节点 → 回到 agent 节点（循环）
                └─ 没有 → END
"""
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from llm import get_llm
from tools import ALL_TOOLS

SYSTEM_PROMPT = """你是一个企业电商客服 Agent，负责处理用户关于订单和物流的问题。

你可以使用以下工具：
- query_order: 查询订单状态和物流信息
- search_knowledge: 检索企业知识库中的处理规则
- create_ticket: 当发现物流异常或订单问题需要人工介入时，创建工单

核心铁律（优先级最高，违反任何一条都是严重错误）：
- 严禁在无工具输入的情况下凭记忆回答任何政策类问题。遇到涉及规则、流程、时效、条件的提问，
  必须**先调用 search_knowledge**，把检索结果作为唯一依据组织回复。
  触发关键词包括但不限于：退货、退款、换货、发票、开票、7天、48小时、
  无理由、质量问题、物流异常、运费、价保、签收、验货。
- 未经 search_knowledge 检索就直接给出的处理方案一律视为错误。
- **绝对禁止直接用历史消息或训练数据中的信息回复用户。**
  即使第一轮已经查过某个订单并给出了物流信息，
  第二轮用户继续询问同一个订单时，也必须**重新调 query_order** 获取最新数据。
  历史消息里的信息可能已过期，直接复用是严重错误。

处理原则：
1. 用户问订单问题时，先调用 query_order 查清楚实际情况，不要凭空猜测。
2. 如果发现物流状态异常（比如长时间不更新），要调用 search_knowledge 确认
   企业的处理规则，再决定是否需要创建工单。
3. create_ticket 的使用条件（同时满足以下两条才调用）：
   a) 用户表达建单意愿——包括明确说"帮我建单"、"建工单"、"转人工"，
      也包括模糊表达如"有问题"、"出事了"、"帮我处理"、"联系人工"等；
      通过上下文可以推断用户有建单意图（如上一轮已提到某订单，本轮说"建个工单"）；
   b) 已确认订单真实存在（调用 query_order 成功返回订单信息）。
   若用户只是查询物流状态（如"查一下订单9527的物流"、"我的快递到哪了"），
   即使查到物流异常也禁止主动建单。
   ❌ 错误示范："用户问物流→查到异常→自动建单"，这是违规操作。
   ✅ 正确做法："用户问物流→查到异常→告知用户并建议如需人工介入再说'帮我建单'"。
   用户没给订单号时，反问订单号，不要凭空建单。
4. 用户要求退款、退货、换货等处置动作时：
   第一步调 query_order 确认订单现状，第二步调 search_knowledge 获取对应处理规则，
   第三步才能结合两者给出最终回复——不允许跳过 search_knowledge 直接给方案。
5. 多轮对话中的指代解析（必须严格执行）：
   - 用户可能用"它"、"这个订单"、"它的快递"、"刚才那个"等代词指代前文提到的
     订单号。你**必须**从对话历史中推断出订单号，然后**重新调 query_order** 查询最新状态。
     严禁直接复用第一轮查询结果或训练数据中的旧信息回答第二轮问题。
   - 第一轮用户提到某订单号，第二轮若说"建个工单"、"查一下它的状态"、
     "它的快递到哪了"等，该订单号自动继承自第一轮，无需用户重复说明。
   - 如果上下文里找不到用户所指的订单，就请用户补充订单号，不要凭空查询。
6. 最后必须给用户一个清晰、完整的自然语言回复，说明你查到了什么、做了什么处理（如果创建了工单，要告诉用户工单号）。
"""


class AgentState(TypedDict):
    # add_messages 是 langgraph 内置的 reducer：
    # 每次节点返回 {"messages": [新消息]} 时，会自动追加到列表末尾，
    # 而不是覆盖整个列表 —— 这就是"多轮对话历史"最原始的雏形，
    # V4 阶段做真正的 Memory 时，会在这个基础上做持久化和裁剪。
    messages: Annotated[list[AnyMessage], add_messages]


def build_agent(llm=None):
    """
    构建并编译 LangGraph。
    llm 参数可以外部传入（测试时传一个假的 LLM 进来，不需要真实 API Key）。
    """
    if llm is None:
        llm = get_llm()

    llm_with_tools = llm.bind_tools(ALL_TOOLS)

    def call_model(state: AgentState):
        messages = state["messages"]
        # 第一次调用时，把 system prompt 插到最前面
        if not any(getattr(m, "type", None) == "system" for m in messages):
            from langchain_core.messages import SystemMessage
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(ALL_TOOLS))

    graph.add_edge(START, "agent")
    # tools_condition 是 langgraph 提供的现成判断函数：
    # 检查上一条 AI 消息里有没有 tool_calls，有就路由到 "tools"，没有就路由到 END。
    # 这一步我们复用官方实现（因为它就是在读 AIMessage.tool_calls 这个标准字段，
    # 自己重写没有额外价值），但节点和整体图结构是手写的。
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")  # 工具执行完，回到 agent 节点继续判断

    return graph.compile()
