"""优化建议的数据形状：Suggestion / Severity / Category。frozen，纯 stdlib。

纯静态分析（零 API、不碰真实环境）的产物：每条建议带稳定 rule_id（可 grep）、严重度、类别、
人类可读的标题与详情、以及涉及的节点 id。env 上限可注入（见 rules.py 的 OptimizeEnv），
不读真实环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """建议严重度（排序用：HIGH 在前）。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(str, Enum):
    """建议类别。"""

    PARALLEL = "parallel"      # 并行机会
    BOTTLENECK = "bottleneck"  # 串行瓶颈
    CACHE = "cache"            # 缓存机会
    RESOURCE = "resource"      # 资源配置
    LLM = "llm"                # LLM 配置


# 严重度排序权重（数值越小越靠前）。
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.HIGH: 0,
    Severity.MEDIUM: 1,
    Severity.LOW: 2,
    Severity.INFO: 3,
}


@dataclass(frozen=True)
class Suggestion:
    """一条静态优化建议。"""

    rule_id: str
    severity: Severity
    category: Category
    title: str
    detail: str = ""
    node_ids: tuple[str, ...] = field(default_factory=tuple)

    def sort_key(self) -> tuple[int, str]:
        """排序键：先按严重度，再按 rule_id（确定性）。"""
        return (_SEVERITY_ORDER[self.severity], self.rule_id)
