"""analyzer：跑全部静态规则，汇聚 → 确定性排序 → 去重，吐最终建议列表。

零 API、纯静态。env 上限可注入（OptimizeEnv），默认用其默认值。排序键见 Suggestion.sort_key
（先严重度后 rule_id）；去重按 (rule_id, node_ids) 摘除完全相同的重复项。
"""

from __future__ import annotations

from ragspine.dify.ir.model import WorkflowIR
from ragspine.dify.optimize.rules import ALL_RULES, OptimizeEnv
from ragspine.dify.optimize.suggestion import Suggestion


def analyze_ir(ir: WorkflowIR, *, env: OptimizeEnv | None = None) -> list[Suggestion]:
    """跑全部静态规则，汇聚并按 (severity, rule_id) 确定性排序，去重后返回。"""
    env = env or OptimizeEnv()

    collected: list[Suggestion] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for rule in ALL_RULES:
        for s in rule(ir, env):
            key = (s.rule_id, s.node_ids)
            if key in seen:
                continue
            seen.add(key)
            collected.append(s)

    collected.sort(key=lambda s: s.sort_key())
    return collected
