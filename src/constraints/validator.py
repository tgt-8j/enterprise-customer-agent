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

# 工单号模式：必须带"工单"或 TC 前缀，绝不匹配裸数字。
# 旧实现是 (?:TC\d{6,12}|\d{1,20})，任何数字都算命中：
# 回复里出现"48小时""7天""2026年"就会让「已建单必须带工单号」这条校验恒为通过，
# 连带 AutoFixer 补工单号也永远不会触发 —— README 主推的特性是死代码。
TICKET_ID_RE = re.compile(r"TC\d{6,12}|工单\s*号?\s*[:：]?\s*\d{1,20}")

# 政策来源标注模式：[来源: xxx] 或 "根据《xxx》"
_POLICY_SOURCE_RE = re.compile(r"\[来源:.*?\]|根据《[^》]+》")


def contains_ticket_id(output: str, ticket_id: str | None) -> bool:
    """判断回复里是否提到了「本次创建的那个工单号」。

    有 ticket_id 时按精确数字边界匹配（"工单号 1" 命中，"12" 不会命中 "1"）；
    没有 ticket_id（例如只传了 ticket_created）时退回带"工单/TC"锚点的通用模式。
    这样"48小时"这类普通数字不会被误判成工单号。
    """
    if ticket_id:
        return re.search(rf"(?<!\d){re.escape(ticket_id)}(?!\d)", output) is not None
    return TICKET_ID_RE.search(output) is not None


# 退款承诺短语（用于 promise_refund_without_verification）
_REFUND_PROMISE_RE = re.compile(r"可以退|给您退|全额退|马上退|立即退")
# 出现这些否定词时，前面的"可以退"是政策条款而不是承诺
_NEGATION_PREFIXES = ("不", "无法", "不能", "暂", "尚未")

# 具体订单事实（用于 fabricate_order_info）：订单号 / 订单状态 / 物流进展
_FABRICATE_ORDER_INFO_RE = re.compile(
    r"订单号\s*[:：]?\s*\d{6,}"
    r"|订单状态为"
    r"|您的订单(?:已经|已)(?:发货|签收|到达|出库|完成|取消)"
    r"|物流(?:信息|轨迹|状态)?显示"
)


def _has_unnegated_refund_promise(output: str) -> bool:
    """是否出现「未被否定的」退款承诺。

    "不可以退" / "无法给您退" 是政策条款，不算承诺；
    "可以给您全额退款" 才是承诺。
    """
    for match in _REFUND_PROMISE_RE.finditer(output):
        prefix = output[max(0, match.start() - 4) : match.start()]
        if any(neg in prefix for neg in _NEGATION_PREFIXES):
            continue
        return True
    return False


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

    def __init__(self, constraints_file: str | None = None) -> None:
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
        return dict(agents.get(agent_id) or {})

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
        if "ask_for_password_or_ssn" in forbidden_actions:
            # 只检测"索要/要求提供"类表达，不匹配"已存储/不会透露"等被动描述。
            # 旧正则里的 [密码] 是字符类（等于"密"或"码"），"请稍等，我在查订单编码"
            # 都会被判成索要密码；这里改成"请求动词 + 敏感信息名词"的组合。
            # 这条规则是 error 级、现在会真的拦截回复，所以精度必须够。
            if re.search(
                r"请.{0,6}?(提供|输入|告知|告诉|发送|填写|核对).{0,10}?"
                r"(密码|验证码|身份证|SSN|社保号)|索要.{0,6}?(密码|身份证|SSN|验证码)",
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
            # 检测到明确承诺退款但上下文中没有订单核实信息。
            # 必须排除否定式（"已拆封的商品不可以退"），否则政策类回答会被误拦；
            # 这条规则是 error 级、现在会真的替换回复，误报的代价比以前大得多。
            if _has_unnegated_refund_promise(output) and not context.get("order_verified"):
                violations.append(
                    Violation(
                        rule="promise_refund_without_verification",
                        severity="error",
                        detail="输出中承诺退款但未核实订单信息",
                    )
                )

        if "fabricate_order_info" in forbidden_actions:
            # 检测到明确陈述具体订单事实（订单号/状态/物流进展）但上下文中没有核实依据。
            # 收窄到"具体的订单事实"，不再用裸的"您的订单已"：政策类回答里
            # "您的订单已超过7天，不符合无理由退货条件"会被这句话误伤。
            if _FABRICATE_ORDER_INFO_RE.search(output) and not context.get("order_verified"):
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
            if context.get("ticket_created") and not contains_ticket_id(
                output, context.get("ticket_id")
            ):
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
