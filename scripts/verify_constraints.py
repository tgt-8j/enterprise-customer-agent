#!/usr/bin/env python
"""
约束验证演示脚本：独立于应用运行，直接展示 Validator 和 AutoFixer 的行为。

用法：
    python scripts/verify_constraints.py
"""

import sys
from pathlib import Path

# 确保项目根目录在 PYTHONPATH 中（scripts/ 不在根目录）
sys.path.insert(0, str(Path(__file__).parent.parent))

from constraints.validator import ConstraintValidator
from constraints.auto_fixer import AutoFixer


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    validator = ConstraintValidator()
    fixer = AutoFixer()

    # ── 工具白名单 ──────────────────────────────────────────────
    section("1. 工具调用白名单验证")

    for tool in ["query_order", "search_knowledge", "create_ticket"]:
        r = validator.validate_tool_call("customer_service", tool)
        print(f"  {tool:20s} → {'✓ 允许' if r.valid else '✗ 拒绝'}")

    r = validator.validate_tool_call("customer_service", "delete_order")
    print(f"  {'delete_order':20s} → {'✓ 允许' if r.valid else '✗ 拒绝'} (error: 不在白名单)")

    r = validator.validate_tool_call("chitchat", "query_order")
    print(f"  chitchat+query_order → {'✓ 允许' if r.valid else '✗ 拒绝'} (error: 闲聊不允许工具)")

    # ── 输出验证：正常场景 ──────────────────────────────────────
    section("2. 正常回复（无违规）")

    normal_reply = "您的订单9527目前在转运中心，预计1-3天内送达，请耐心等待。"
    r = validator.validate_output("customer_service", normal_reply)
    print(f"  输出: {normal_reply[:50]}...")
    print(f"  结果: {'✓ 通过' if r.valid else '✗ 阻断'} | 违规: {len(r.violations)} | 可修复: {r.auto_fixable}")

    # ── 输出验证：工单号缺失（可自动修复）───────────────────────
    section("3. 工单创建后未带工单号（自动修复）")

    ticket_reply = "您的工单已受理，我们会尽快处理，请保持电话畅通。"
    r = validator.validate_output(
        "customer_service", ticket_reply,
        context={"ticket_created": True, "ticket_id": "TC123456"}
    )
    print(f"  原始输出: {ticket_reply}")
    print(f"  违规: {[v.rule for v in r.violations]}")
    print(f"  可修复: {r.auto_fixable}")
    if r.auto_fixable:
        fixed = fixer.fix_output(ticket_reply, r.auto_fixable, context={"ticket_id": "TC123456"})
        print(f"  修复后: {fixed}")

    # ── 输出验证：绝对化用语（自动修复）─────────────────────────
    section("4. 绝对化用语承诺（自动软化）")

    absolute_reply = "我们保证您的快递明天一定送达！"
    r = validator.validate_output("customer_service", absolute_reply)
    print(f"  原始输出: {absolute_reply}")
    print(f"  违规: {[v.rule for v in r.violations]}")
    if r.auto_fixable:
        fixed = fixer.fix_output(absolute_reply, r.auto_fixable)
        print(f"  修复后: {fixed}")

    # ── 输出验证：索要密码（阻断）───────────────────────────────
    section("5. 索要密码（error 级阻断）")

    bad_reply = "为了核实身份，请提供您的密码和身份证号。"
    r = validator.validate_output("customer_service", bad_reply)
    print(f"  输出: {bad_reply}")
    print(f"  结果: {'✓ 通过' if r.valid else '✗ 阻断'} (is_blockable={r.is_blockable})")
    print(f"  违规: {[(v.rule, v.severity) for v in r.violations]}")

    # ── 输出验证：承诺退款未核实订单（阻断）─────────────────────
    section("6. 承诺退款但未核实订单（error 级阻断）")

    refund_reply = "没问题，我们可以给您全额退款。"
    r = validator.validate_output(
        "customer_service", refund_reply,
        context={"order_verified": False}
    )
    print(f"  输出: {refund_reply}")
    print(f"  结果: {'✓ 通过' if r.valid else '✗ 阻断'} (is_blockable={r.is_blockable})")
    print(f"  违规: {[(v.rule, v.severity) for v in r.violations]}")

    # ── 输出验证：政策引用无来源（warning，可修复）──────────────
    section("7. 政策回复未标注来源（warning，可修复）")

    policy_reply = "根据规定，7天内可以申请无理由退货，运费由我们承担。"
    r = validator.validate_output("customer_service", policy_reply)
    print(f"  输出: {policy_reply}")
    print(f"  违规: {[v.rule for v in r.violations]}")
    if r.auto_fixable:
        fixed = fixer.fix_output(policy_reply, r.auto_fixable)
        print(f"  修复后: {fixed}")

    # ── 输出验证：超长回复（自动截断）───────────────────────────
    section("8. 超长回复（自动截断）")

    long_reply = "好的，关于您的问题，我来详细说明一下。" + "重复内容 " * 500
    r = validator.validate_output("customer_service", long_reply)
    print(f"  原始长度: {len(long_reply)} 字（限制 1500）")
    print(f"  违规: {[v.rule for v in r.violations]}")
    if r.auto_fixable:
        fixed = fixer.fix_output(long_reply, r.auto_fixable)
        print(f"  修复后长度: {len(fixed)} 字")

    # ── 综合场景：多条违规同时发生 ──────────────────────────────
    section("9. 综合场景：多条违规同时触发")

    chaos_reply = "我们保证明天一定送达！请提供您的密码。" + "x" * 1600
    r = validator.validate_output("customer_service", chaos_reply)
    print(f"  输出长度: {len(chaos_reply)} 字")
    print(f"  违规 ({len(r.violations)} 条):")
    for v in r.violations:
        print(f"    - [{v.severity}] {v.rule}: {v.detail[:50]}")
    print(f"  可修复: {r.auto_fixable}")
    if r.auto_fixable:
        fixed = fixer.fix_output(chaos_reply, r.auto_fixable)
        print(f"  修复后长度: {len(fixed)} 字")
        has_guarantee = "保证" in fixed or "一定" in fixed
        print(f"  绝对化用语已清除: {not has_guarantee}")

    print(f"\n{'='*60}")
    print("  演示完毕")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
