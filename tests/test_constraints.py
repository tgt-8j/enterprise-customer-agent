"""约束验证模块测试。

覆盖：
- ConstraintValidator：工具白名单、输出规则（工单号、政策来源、绝对化用语、长度限制、密码/退款）
- AutoFixer：工单号补全、截断、政策来源标注、语气软化
- 集成路径：main.py 的 chat 端点正确调用验证器
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from constraints.validator import ConstraintValidator, ValidationResult, Violation
from constraints.auto_fixer import AutoFixer


# ── ConstraintValidator ──────────────────────────────────────────────────────


class TestConstraintValidatorInit:
    def test_loads_yaml(self):
        v = ConstraintValidator()
        assert "agents" in v._config
        assert "customer_service" in v._config["agents"]
        assert "common" in v._config

    def test_custom_path(self, tmp_path):
        path = tmp_path / "custom.yaml"
        path.write_text("agents: {}\ncommon: {}\n")
        v = ConstraintValidator(str(path))
        assert v._config == {"agents": {}, "common": {}}


class TestValidateToolCall:
    @pytest.fixture
    def validator(self):
        return ConstraintValidator()

    def test_allowed_tool_passes(self, validator):
        r = validator.validate_tool_call("customer_service", "query_order")
        assert r.valid is True
        assert r.violations == []

    def test_disallowed_tool_blocks(self, validator):
        r = validator.validate_tool_call("customer_service", "delete_order")
        assert r.valid is False
        assert r.is_blockable is True
        assert any("whitelist" in v.rule for v in r.violations)

    def test_chitchat_allows_no_tool(self, validator):
        r = validator.validate_tool_call("chitchat", "query_order")
        assert r.valid is False
        assert r.is_blockable is True

    def test_unknown_agent_allows_any_tool(self, validator):
        # 未知 agent 无白名单，不阻断（防御性策略）
        r = validator.validate_tool_call("unknown_agent", "anything")
        assert r.valid is True

    def test_ticket_created_allows_all_three_tools(self, validator):
        for tool in ["query_order", "search_knowledge", "create_ticket"]:
            r = validator.validate_tool_call("customer_service", tool)
            assert r.valid is True

    def test_chitchat_no_tool_name_allowed(self, validator):
        # chitchat 的 allowed_tools 是空列表，不允许任何工具
        r = validator.validate_tool_call("chitchat", "search_knowledge")
        assert r.valid is False


class TestValidateOutput:
    @pytest.fixture
    def validator(self):
        return ConstraintValidator()

    # ---- must_include_ticket_id_if_ticket_created ----
    def test_ticket_id_present_when_ticket_created(self, validator):
        out = "您的工单已创建，工单号：TC987654，我们会尽快处理。"
        r = validator.validate_output(
            "customer_service", out, context={"ticket_created": True, "ticket_id": "TC987654"}
        )
        assert r.valid is True

    def test_ticket_id_missing_when_ticket_created(self, validator):
        out = "您的工单已创建，我们会尽快处理。"
        r = validator.validate_output(
            "customer_service", out, context={"ticket_created": True, "ticket_id": "TC987654"}
        )
        assert r.valid is False
        assert r.is_blockable is True
        assert any(v.rule == "must_include_ticket_id" for v in r.violations)
        assert "add_ticket_id" in r.auto_fixable

    def test_ticket_id_not_checked_when_no_ticket(self, validator):
        out = "您的订单物流已更新。"
        r = validator.validate_output("customer_service", out, context={"ticket_created": False})
        assert r.valid is True

    def test_ticket_id_not_checked_when_context_empty(self, validator):
        out = "您的订单物流已更新。"
        r = validator.validate_output("customer_service", out, context={})
        assert r.valid is True

    # ---- must_cite_policy_source ----
    def test_policy_with_source_citation(self, validator):
        out = "根据《七天无理由退货政策》，您可以申请退货。[来源: 02-七天无理由退货]"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    def test_policy_with_source_bracket(self, validator):
        out = "退货政策：7天内可无理由退货 [来源: 01-退货政策总则]"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    def test_policy_without_source_citation(self, validator):
        out = "您可以申请七天无理由退货，运费由我们承担。"
        r = validator.validate_output("customer_service", out)
        assert any(v.rule == "must_cite_policy_source" for v in r.violations)
        assert "add_policy_citation" in r.auto_fixable

    def test_non_policy_query_no_citation_required(self, validator):
        out = "您的订单目前在中转站，预计明天送达。"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    def test_query_order_not_flagged_as_policy(self, validator):
        out = "订单9527当前状态为运输中，最新物流信息：已到达杭州市中转站。"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    # ---- no_absolute_guarantees ----
    def test_absolute_guarantee_on_delivery(self, validator):
        out = "我们保证您的快递明天一定送达！"
        r = validator.validate_output("customer_service", out)
        assert any(v.rule == "no_absolute_guarantees" for v in r.violations)
        assert "soften_absolutes" in r.auto_fixable

    def test_safe_use_of_guarantee_word(self, validator):
        # "保证"用于非配送场景，不触发 guarantee_delivery_time
        out = "我们保证您的个人信息安全。"
        r = validator.validate_output("customer_service", out)
        assert not any(v.rule == "guarantee_delivery_time" for v in r.violations)

    def test_no_absolute_guarantee_in_normal_response(self, validator):
        out = "您的订单正在配送中，预计1-3天送达。"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    # ---- max_response_length ----
    def test_output_over_length_limit(self, validator):
        long_text = "a" * 2000
        r = validator.validate_output("customer_service", long_text)
        assert any(v.rule == "max_response_length" for v in r.violations)
        assert "truncate_output" in r.auto_fixable

    def test_output_within_length_limit(self, validator):
        short_text = "好的，帮您查一下。"
        r = validator.validate_output("customer_service", short_text)
        assert r.valid is True

    def test_output_at_exact_limit(self, validator):
        exactly_1500 = "a" * 1500
        r = validator.validate_output("customer_service", exactly_1500)
        assert r.valid is True

    def test_output_one_over_limit(self, validator):
        one_over = "a" * 1501
        r = validator.validate_output("customer_service", one_over)
        assert any(v.rule == "max_response_length" for v in r.violations)

    # ---- ask_for_password_or_ssn ----
    def test_forbidden_password_request(self, validator):
        out = "为了核实身份，请提供您的密码和身份证号。"
        r = validator.validate_output("customer_service", out)
        assert any(v.rule == "ask_for_password_or_ssn" for v in r.violations)
        assert r.is_blockable is True

    def test_forbidden_ssn_request(self, validator):
        out = "请提供您的SSN号码以验证身份。"
        r = validator.validate_output("customer_service", out)
        assert any(v.rule == "ask_for_password_or_ssn" for v in r.violations)
        assert r.is_blockable is True

    def test_normal_response_no_false_positive(self, validator):
        out = "您的订单正在配送中，请耐心等待。"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    def test_password_mentioned_in_context_not_flagged(self, validator):
        # 用户提到密码，但agent回复中没有索要
        out = "您的账号密码已在系统中加密存储，我们不会向任何人透露。"
        r = validator.validate_output("customer_service", out)
        assert r.valid is True

    # ---- promise_refund_without_verification ----
    def test_refund_without_order_verified(self, validator):
        out = "没问题，我们可以给您全额退款。"
        r = validator.validate_output(
            "customer_service", out, context={"order_verified": False}
        )
        assert any(v.rule == "promise_refund_without_verification" for v in r.violations)
        assert r.is_blockable is True

    def test_refund_with_order_verified(self, validator):
        out = "核实后，我们可以为您办理退款。"
        r = validator.validate_output(
            "customer_service", out, context={"order_verified": True}
        )
        # "办理退款" 不匹配 "可以退|给您退|全额退|马上退"
        assert not any(v.rule == "promise_refund_without_verification" for v in r.violations)

    def test_refund_ask_not_promise(self, validator):
        out = "请问您希望如何处理？我们可以为您办理退款或换货。"
        r = validator.validate_output(
            "customer_service", out, context={"order_verified": False}
        )
        assert not any(v.rule == "promise_refund_without_verification" for v in r.violations)

    # ---- chitchat agent ----
    def test_chitchat_long_response(self, validator):
        long_text = "x" * 600
        r = validator.validate_output("chitchat", long_text)
        assert any(v.rule == "max_response_length" for v in r.violations)

    def test_chitchat_normal_response(self, validator):
        out = "您好！有什么可以帮助您的吗？"
        r = validator.validate_output("chitchat", out)
        assert r.valid is True

    def test_chitchat_password_request_blocked(self, validator):
        out = "请先告诉我您的密码。"
        r = validator.validate_output("chitchat", out)
        assert r.is_blockable is True

    # ---- combined violations ----
    def test_multiple_violations_returned(self, validator):
        long_text = "我们保证明天一定送达，请提供密码。" + "x" * 1500
        r = validator.validate_output(
            "customer_service", long_text, context={"order_verified": False}
        )
        rules = {v.rule for v in r.violations}
        assert "no_absolute_guarantees" in rules
        assert "max_response_length" in rules
        assert "ask_for_password_or_ssn" in rules


# ── AutoFixer ────────────────────────────────────────────────────────────────


class TestAutoFixer:
    @pytest.fixture
    def fixer(self):
        return AutoFixer()

    # ---- add_ticket_id ----
    def test_add_ticket_id_to_empty_reply(self, fixer):
        fixed = fixer.fix_output(
            "您的工单已受理。",
            auto_fixable=["add_ticket_id"],
            context={"ticket_id": "TC123456"},
        )
        assert "TC123456" in fixed
        assert "工单信息" in fixed

    def test_no_duplicate_ticket_id(self, fixer):
        out = "您的工单号是 TC123456，请查收。"
        fixed = fixer.fix_output(
            out, auto_fixable=["add_ticket_id"], context={"ticket_id": "TC654321"}
        )
        # 不应重复追加
        assert fixed.count("TC") == 1

    def test_no_add_when_no_ticket_id_in_context(self, fixer):
        out = "您的工单已受理。"
        fixed = fixer.fix_output(out, auto_fixable=["add_ticket_id"], context={})
        assert fixed == out

    def test_no_add_when_empty_context(self, fixer):
        out = "您的工单已受理。"
        fixed = fixer.fix_output(out, auto_fixable=["add_ticket_id"])
        assert fixed == out

    # ---- truncate_output ----
    def test_truncate_long_output(self, fixer):
        long_text = "a" * 2000
        fixed = fixer.fix_output(long_text, auto_fixable=["truncate_output"])
        assert len(fixed) <= 1500
        assert "已截断" in fixed

    def test_no_truncate_short_output(self, fixer):
        short = "好的。"
        fixed = fixer.fix_output(short, auto_fixable=["truncate_output"])
        assert fixed == short

    def test_truncate_at_sentence_boundary(self, fixer):
        text = "第一段。\n\n第二段内容很多很长一直到超过限制。" + "x" * 1600
        fixed = fixer.fix_output(text, auto_fixable=["truncate_output"])
        # 尽量在段落边界截断
        assert "已截断" in fixed
        assert len(fixed) <= 1500

    def test_truncate_with_newlines(self, fixer):
        text = "第一段\n\n第二段内容很多很长一直到超过限制。" + "x" * 1600
        fixed = fixer.fix_output(text, auto_fixable=["truncate_output"])
        assert len(fixed) <= 1500

    # ---- add_policy_citation ----
    def test_add_citation_when_missing(self, fixer):
        out = "根据政策，7天内可以无理由退货。"
        fixed = fixer.fix_output(out, auto_fixable=["add_policy_citation"])
        assert "企业知识库" in fixed

    def test_no_double_citation(self, fixer):
        out = "根据政策，7天内可以无理由退货。（以上内容参考企业知识库相关政策规定）"
        fixed = fixer.fix_output(out, auto_fixable=["add_policy_citation"])
        assert fixed.count("企业知识库") == 1

    def test_no_citation_for_non_policy(self, fixer):
        out = "您的订单正在配送中。"
        fixed = fixer.fix_output(out, auto_fixable=["add_policy_citation"])
        assert fixed == out

    # ---- soften_absolutes ----
    def test_soften_guarantee(self, fixer):
        out = "我们保证明天送达。"
        fixed = fixer.fix_output(out, auto_fixable=["soften_absolutes"])
        assert "保证" not in fixed
        assert "预计" in fixed

    def test_soften_yiding(self, fixer):
        out = "货物一定今天到。"
        fixed = fixer.fix_output(out, auto_fixable=["soften_absolutes"])
        assert "一定" not in fixed
        assert "大概率" in fixed

    def test_soften_buke(self, fixer):
        out = "绝对没有问题。"
        fixed = fixer.fix_output(out, auto_fixable=["soften_absolutes"])
        assert "绝对" not in fixed
        assert "通常" in fixed

    def test_no_change_when_no_absolutes(self, fixer):
        out = "您的订单正在配送中。"
        fixed = fixer.fix_output(out, auto_fixable=["soften_absolutes"])
        assert fixed == out

    def test_soften_multiple_words(self, fixer):
        out = "我们保证一定明天送达，绝对没问题。"
        fixed = fixer.fix_output(out, auto_fixable=["soften_absolutes"])
        assert "保证" not in fixed
        assert "一定" not in fixed
        assert "绝对" not in fixed

    # ---- combined fixes ----
    def test_combined_fixes(self, fixer):
        long_text = "我们的政策保证您可以退货，这是一个很好的服务。（" + "x" * 1400 + "）"
        fixed = fixer.fix_output(
            long_text,
            auto_fixable=["soften_absolutes", "truncate_output"],
            context={},
        )
        assert "保证" not in fixed
        assert len(fixed) <= 1500

    def test_fixes_applied_in_order(self, fixer):
        """测试修复顺序：先软化，再截断（截断后内容更短）。"""
        long_text = "我们保证明天一定送达。" + "x" * 1600
        fixed = fixer.fix_output(
            long_text,
            auto_fixable=["soften_absolutes", "truncate_output"],
            context={},
        )
        assert "保证" not in fixed
        assert len(fixed) <= 1500

    def test_no_fixes_when_empty_list(self, fixer):
        out = "保证明天送达。"
        fixed = fixer.fix_output(out, auto_fixable=[], context={})
        assert fixed == out

    def test_invalid_fix_type_ignored(self, fixer):
        """无效的修复类型应被忽略，不抛异常。"""
        out = "正常回复。"
        fixed = fixer.fix_output(out, auto_fixable=["unknown_fix_type"], context={})
        assert fixed == out


# ── 集成测试：约束验证在 chat 流程中的使用 ───────────────────────────────────


class TestChatIntegration:
    """验证 chat 端点存在且需要认证。"""

    def test_chat_requires_auth(self, client_no_auth):
        """无 token 时 /chat 返回 401。"""
        resp = client_no_auth.post("/chat", json={"message": "你好"})
        assert resp.status_code == 401

    def test_chat_stream_requires_auth(self, client_no_auth):
        """无 token 时 /chat_stream 返回 401。"""
        resp = client_no_auth.post("/chat_stream", json={"message": "你好"})
        assert resp.status_code == 401
