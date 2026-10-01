"""
自动修复器：对可自动修复的约束违规进行兜底处理。

修复策略：
- 确定性修复：工单号补全、文本截断 —— 零风险，直接做
- 提示性修复：政策来源标注 —— 追加固定格式的来源说明
- 语气软化：绝对化用语替换 —— 用"预计""大概率"替代"保证""一定"

不做的修复：
- 不改动事实性内容（如订单状态、物流信息）
- 不使用 LLM 重写（会引入新的不确定性）

与 MediX AutoFixer 的对比：
- MediX 用字符串拼接硬编码免责声明（"以上信息仅供参考..."）
- 本实现针对客服场景定制，修复逻辑有业务语义
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 绝对化用语 → 软化版本映射
_SOFTEN_MAP: dict[str, str] = {
    "保证": "预计",
    "一定": "大概率",
    "绝对": "通常",
    "100%": "几乎",
    "必然": "很可能",
    "肯定": "应该",
}


class AutoFixer:
    """
    根据 ValidationResult.auto_fixable 列表执行自动修复。

    用法：
        fixer = AutoFixer()
        fixed = fixer.fix_output(
            output="原始回复",
            auto_fixable=["add_ticket_id", "soften_absolutes"],
            context={"ticket_id": "TC123456"},
        )
    """

    def fix_output(
        self,
        output: str,
        auto_fixable: list[str],
        context: dict | None = None,
    ) -> str:
        """
        执行所有可自动修复的变更，返回修复后的文本。

        Args:
            output: Agent 原始输出
            auto_fixable: 可修复的违规类型列表
            context: 验证上下文（含 ticket_id 等）

        Returns:
            修复后的输出（可能未改动）
        """
        context = context or {}
        fixed = output

        for fix_type in auto_fixable:
            original = fixed
            if fix_type == "add_ticket_id":
                fixed = self._add_ticket_id(fixed, context)
            elif fix_type == "truncate_output":
                fixed = self._truncate(fixed)
            elif fix_type == "add_policy_citation":
                fixed = self._add_policy_citation(fixed)
            elif fix_type == "soften_absolutes":
                fixed = self._soften_absolutes(fixed)

            if fixed != original:
                logger.info("自动修复应用: %s", fix_type)

        return fixed

    def _add_ticket_id(self, output: str, context: dict) -> str:
        """在回复末尾补充工单号。"""
        ticket_id = context.get("ticket_id", "")
        if not ticket_id:
            return output
        # 避免重复追加
        if re.search(r"TC\d{6,12}", output):
            return output
        suffix = f"\n\n【工单信息】您的工单号：{ticket_id}，我们将在 24 小时内处理并短信通知您。"
        logger.debug("自动添加工单号: %s", ticket_id)
        return output + suffix

    def _truncate(self, output: str, max_length: int = 1500) -> str:
        """按字数截断，尽量在句号处断开。"""
        if len(output) <= max_length:
            return output
        # 向前找最近的句号/换行，避免截在半句话中间
        cutoff = max_length - 20  # 留 20 字给提示语
        for sep in ["\n\n", "。", "！", "？"]:
            idx = output.rfind(sep, 0, cutoff)
            if idx > max_length * 0.5:  # 至少保留一半内容
                return output[: idx + len(sep)] + "\n\n[回复内容较长，已截断]"
        return output[:cutoff] + "\n\n[回复内容较长，已截断]"

    def _add_policy_citation(self, output: str) -> str:
        """
        在政策类回复末尾追加来源提示。
        仅当输出包含政策相关关键词时才添加，避免对普通回复误加。
        """
        policy_keywords = ["退货", "退款", "换货", "发票", "7天", "无理由", "政策", "规则"]
        if not any(kw in output for kw in policy_keywords):
            return output
        suffix = "\n\n（以上内容参考企业知识库相关政策规定）"
        if "企业知识库" in output:
            return output
        logger.debug("自动添加政策来源提示")
        return output + suffix

    def _soften_absolutes(self, output: str) -> str:
        """将绝对化用语替换为更保守的表达。"""
        for hard, soft in _SOFTEN_MAP.items():
            output = output.replace(hard, soft)
        logger.debug("自动软化绝对化用语")
        return output
