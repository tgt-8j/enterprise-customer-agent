"""意图分发测试：验证 classify_intent 的分类准确性。"""

import pytest

from src.dispatch.dispatch import DispatchDecision, classify_intent


@pytest.mark.asyncio
async def test_classify_query_order():
    """查询订单意图：包含物流、订单号等关键词。"""
    decision = await classify_intent("查一下订单123456的物流")
    assert decision.intent == "query_order"
    assert decision.confidence == 0.85
    assert decision.extracted_order_id == "123456"


@pytest.mark.asyncio
async def test_classify_query_order_with_id():
    """提取订单号（6位以上）。"""
    decision = await classify_intent("我的快递到哪了 订单123456")
    assert decision.intent == "query_order"
    assert decision.extracted_order_id == "123456"


@pytest.mark.asyncio
async def test_classify_knowledge_search():
    """知识查询意图：涉及退货、退款、发票等政策问题。"""
    decision = await classify_intent("质量问题退货要多久内申请？")
    assert decision.intent == "knowledge_search"
    assert decision.confidence == 0.85


@pytest.mark.asyncio
async def test_classify_create_ticket():
    """建工单意图：表达建单、转人工意愿。"""
    decision = await classify_intent("帮我建个工单")
    assert decision.intent == "create_ticket"
    assert decision.confidence == 0.80


@pytest.mark.asyncio
async def test_classify_chitchat():
    """闲聊意图：无明确业务关键词。"""
    decision = await classify_intent("你好，早上好")
    assert decision.intent == "chitchat"
    assert decision.confidence == 0.5


@pytest.mark.asyncio
async def test_chitchat_downgrade_triggers_llm_branch(monkeypatch):
    """修复 #dispatch 乱码死分支后：关键词落到"无匹配关键词"降级分支时，
    确实会升级调用 LLM（此前 chr(0x914) 写错码位导致该判断恒为 False，
    LLM 兜底几乎不触发）。这里把 _classify_by_llm 打桩，断言它被调用了。
    """
    import src.dispatch.dispatch as d

    called = {"n": 0}

    async def fake_llm(query):
        called["n"] += 1
        return DispatchDecision(intent="chitchat", confidence=0.5, reasoning="llm")

    monkeypatch.setattr(d, "_classify_by_llm", fake_llm)
    # "你好" 不含任何业务关键词 → 关键词分支落到 chitchat 降级（reasoning 含"无匹配关键词"）
    await classify_intent("你好")
    assert called["n"] >= 1


@pytest.mark.asyncio
async def test_classify_complex_query():
    """复杂查询：物流异常 + 要求建单。"""
    decision = await classify_intent("订单123456物流没更新三天了，帮我建个工单")
    assert decision.intent == "create_ticket"
    assert decision.reasoning != ""


@pytest.mark.asyncio
async def test_dispatch_decision_model():
    """测试 DispatchDecision 模型的结构。"""
    decision = DispatchDecision(
        intent="query_order",
        confidence=0.9,
        reasoning="用户询问物流",
        extracted_order_id="123456",
    )
    assert decision.intent == "query_order"
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.extracted_order_id == "123456"
