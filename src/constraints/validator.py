"""
约束验证器：运行时检查 Agent 行为是否违反业务安全约束。

设计哲学：
- 约束定义在 YAML 中，不改代码就能调整规则 —— 方便运营/合规团队直接改
- 验证分两层：工具调用层（白名单）+ 输出层（规则匹配）
- 验证结果包含 violations（不可自动修复）和 auto_fixable（可自动修复）
- 本模块永不抛异常：验证失败返回结构化的违反报告，由上层决定是否拦截

与 MediX Agent Swarm 的 validator.py 对比：
- MediX 的 validate_tool_call 只打 warning 不阻断（约束形同虚设）
- MediX 的输出验证全是关键词匹配（"您患有"→"可能存在"的替换太粗糙）
- 本实现：工具违规直接阻断；输出验证分"不可修复"和"可自动修复"两类
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 绝对化用语的正则：匹配"保证"、"一定"、"绝对"等词
# 注意：中文不能用 \b，用简单的子串匹配即可
_ABSOLUTE_GUARANTEE_WORDS = ["保证", "一定", "绝对", "100%", "必然", "肯定"]

# 配送相关关键词
_DELIVERY_KEYWORDS = ["送达", "到达", "到的", "发货", "配送"]

# 工单号模式：兼容两种编号
# - 数据库自增工单号：纯数字（create_ticket 工具当前返回 int 自增 ticket_id）
# - 业务工单号：TC + 6~12 位数字（外部系统/未来业务编号规范）
# 用交替模式让两种格式都能被"包含工单号"检查命中，
# 避免校验器与 create_ticket 的实际返回格式脱节（见 tools.py create_ticket）。
_TICKET_ID_RE = re.compile(r"(?:TC\d{6,12}|\d{1,20})")

# 政策来源标注模式：[来源: xxx] 或 "根据《xxx》"
_POLICY_SOURCE_RE = re.compile(r"\[来源:.*?\]|根据《[^》]+》")


@dataclass
class Violation:
    """一条约束违反记录。"""

    rule: str
    severity: str  # "error" 阻断 / "warning" 仅记录
    detail: str = ""


@dataclass
class ValidationResult:
    """验证结果，包含所有违反项和可自动修复的项。"""

    valid: bool
    violations: list[Violation] = field(default_factory=list)
    auto_fixable: list[str] = field(default_factory=list)

    @property
    def is_blockable(self) -> bool:
        """是否有需要阻断的 error 级违规。"""
        return any(v.severity == "error" for v in self.violations)


class ConstraintValidator:
    """
    约束验证器。

    用法：
        validator = ConstraintValidator()
        # 验证工具调用
        result = validator.validate_tool_call("customer_service", "query_order")
        # 验证输出
        result = validator.validate_output("customer_service", "您的订单...", context={"ticket_created": True})
    """

    def __init__(self, constraints_file: str | None = None):
        path = constraints_file or Path(__file__).parent / "constraints.yaml"
        with open(path, encoding="utf-8") as f:
            self._config: dict[str, Any] = yaml.safe_load(f)
        logger.info("ConstraintValidator 已初始化，加载约束来自 %s", path)

    def _get_agent_config(self, agent_id: str) -> dict[str, Any]:
        agents = self._config.get("agents", {})
        # 兼容"意图名"作为 agent_id 传入的情况：
        # 上游（app.py /chat）传的是意图（query_order / knowledge_search /
        # create_ticket / chitchat），而 constraints.yaml 只定义了
        # customer_service 与 chitchat。若不映射，_get_agent_config 永远
        # 命中不到，业务约束就一条都不会加载（形同虚设）。
        # 这里把"会用到业务工具的意图"统一映射到 customer_service 约束集。
        intent_to_agent = {
            "query_order": "customer_service",
            "knowledge_search": "customer_service",
            "create_ticket": "customer_service",
            "chitchat": "chitchat",
        }
        agent_id = intent_to_agent.get(agent_id, agent_id)
        return agents.get(agent_id, {})

    def validate_tool_call(self, agent_id: str, tool_name: str) -> ValidationResult:
        """
        验证某 Agent 是否可以调用指定工具。

        工具不在白名单中 → error 级违规（直接阻断调用）。
        """
        agent_cfg = self._get_agent_config(agent_id)
        allowed = agent_cfg.get("allowed_tools") or []

        # chitchat 意图不允许调用任何工具
        if agent_id == "chitchat" and tool_name:
            return ValidationResult(
                valid=False,
                violations=[
                    Violation(
                        rule="chitchat_forbidden_tools",
                        severity="error",
                        detail=f"闲聊意图不允许调用工具 {tool_name}",
                    )
                ],
            )

        if allowed and tool_name not in allowed:
            return ValidationResult(
                valid=False,
                violations=[
                    Violation(
                        rule="tool_not_in_whitelist",
                        severity="error",
                        detail=f"工具 {tool_name} 不在 {agent_id} 的白名单中",
                    )
                ],
            )

        return ValidationResult(valid=True)

    def validate_output(
        self,
        agent_id: str,
        output: str,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """
        验证 Agent 输出是否符合约束。

        context 可传入额外信息用于上下文感知验证，例如：
          {"ticket_created": True, "ticket_id": "TC123456"}
          {"policy_sourced": False}
        """
        context = context or {}
        agent_cfg = self._get_agent_config(agent_id)
        common_cfg = self._config.get("common", {})

        output_constraints = (agent_cfg.get("output_constraints") or []) + (
            common_cfg.get("output_constraints") or []
        )
        forbidden_actions = (agent_cfg.get("forbidden_actions") or []) + (
            common_cfg.get("safety_rules") or []
        )

        violations: list[Violation] = []
        auto_fixable: list[str] = []

        # ---- 检查 forbidden_actions（通过输出内容推断）----
        output_lower = output.lower()

        if "ask_for_password_or_ssn" in forbidden_actions:
            # 只检测"索要/要求提供"类表达，不匹配"已存储/不会透露"等被动描述
            if re.search(
                r"(请.*[密码]|提供.*[密码]|请输入密码|SSN|身份证号|索要.*身份)",
                output,
            ):
                violations.append(
                    Violation(
                        rule="ask_for_password_or_ssn",
                        severity="error",
                        detail="输出中包含索要密码或身份证号的内容",
                    )
                )

        if "guarantee_delivery_time" in forbidden_actions:
            has_absolute = any(w in output for w in _ABSOLUTE_GUARANTEE_WORDS)
            has_delivery = any(kw in output for kw in _DELIVERY_KEYWORDS)
            if has_absolute and has_delivery:
                violations.append(
                    Violation(
                        rule="guarantee_delivery_time",
                        severity="error",
                        detail="输出中包含对配送时间的绝对承诺",
                    )
                )

        if "promise_refund_without_verification" in forbidden_actions:
            # 检测到明确承诺退款但上下文中没有订单核实信息
            if re.search(r"可以退|给您退|全额退|马上退", output_lower):
                if not context.get("order_verified"):
                    violations.append(
                        Violation(
                            rule="promise_refund_without_verification",
                            severity="error",
                            detail="输出中承诺退款但未核实订单信息",
                        )
                    )

        if "fabricate_order_info" in forbidden_actions:
            # 检测到明确陈述具体订单事实（订单号/状态/金额）但上下文中没有核实依据
            if re.search(r"(订单号\s*[:：]?\s*\d{6,}|您的订单已|订单状态为)", output):
                if not context.get("order_verified"):
                    violations.append(
                        Violation(
                            rule="fabricate_order_info",
                            severity="error",
                            detail="输出中包含具体订单信息但未核实",
                        )
                    )

        if "disclose_competitor_pricing" in forbidden_actions:
            competitor_keywords = ["竞品", "竞争对手", "某品牌价格", "别家", "友商"]
            pricing_keywords = ["报价", "价格", "成本", "利润"]
            has_competitor = any(kw in output for kw in competitor_keywords)
            has_pricing = any(kw in output for kw in pricing_keywords)
            if has_competitor and has_pricing:
                violations.append(
                    Violation(
                        rule="disclose_competitor_pricing",
                        severity="error",
                        detail="输出中透露了竞品定价信息",
                    )
                )

        if "provide_medical_advice" in forbidden_actions:
            medical_keywords = ["诊断", "处方", "用药", "剂量", "治疗方案", "建议服用"]
            if any(kw in output for kw in medical_keywords):
                violations.append(
                    Violation(
                        rule="provide_medical_advice",
                        severity="error",
                        detail="输出中提供了医疗建议（超出客服职责范围）",
                    )
                )

        # ---- 检查 output_constraints ----
        max_len = self._get_constraint_value(output_constraints, "max_response_length")
        if max_len is not None and len(output) > max_len:
            violations.append(
                Violation(
                    rule="max_response_length",
                    severity="warning",
                    detail=f"输出长度 {len(output)} 超过限制 {max_len} 字",
                )
            )
            auto_fixable.append("truncate_output")

        if "must_include_ticket_id_if_ticket_created" in output_constraints:
            if context.get("ticket_created") and not _TICKET_ID_RE.search(output):
                violations.append(
                    Violation(
                        rule="must_include_ticket_id",
                        severity="error",
                        detail="已创建工单但输出中未包含工单号",
                    )
                )
                auto_fixable.append("add_ticket_id")

        if "must_cite_policy_source" in output_constraints:
            # 如果输出包含政策相关内容但没有来源标注
            if re.search(r"(退货|退款|换货|发票|7天|无理由|政策|规则)", output):
                if not _POLICY_SOURCE_RE.search(output):
                    violations.append(
                        Violation(
                            rule="must_cite_policy_source",
                            severity="warning",
                            detail="政策相关回复未标注来源",
                        )
                    )
                    auto_fixable.append("add_policy_citation")

        if "no_absolute_guarantees" in output_constraints:
            found_words = [w for w in _ABSOLUTE_GUARANTEE_WORDS if w in output]
            if found_words:
                violations.append(
                    Violation(
                        rule="no_absolute_guarantees",
                        severity="warning",
                        detail=f"输出包含绝对化用语：{found_words}",
                    )
                )
                auto_fixable.append("soften_absolutes")

        return ValidationResult(
            valid=not any(v.severity == "error" for v in violations),
            violations=violations,
            auto_fixable=auto_fixable,
        )

    @staticmethod
    def _get_constraint_value(constraints: list[str | dict], key: str) -> Any | None:
        """从约束列表中提取带值的约束（如 max_response_length: 1500）。"""
        for c in constraints:
            if isinstance(c, dict) and key in c:
                return c[key]
        return None
