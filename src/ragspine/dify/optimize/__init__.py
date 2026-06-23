"""optimize 段：WorkflowIR → list[Suggestion]（纯静态规则，零 API）。

只看 IR 的结构（节点类型 / 数据依赖 / parallel_layers / 配置），不跑任何 LLM、不碰真实环境。
8 条规则纯函数：并行机会（PARALLEL_*）/ 串行瓶颈（BOTTLE_*）/ 缓存（CACHE_*）/
资源（RESOURCE_*）/ LLM 配置（LLM_*）。env 上限可注入（OptimizeEnv），不读真实环境变量。

Submodules:
    suggestion.py — Suggestion / Severity / Category 数据形状。
    rules.py      — 8 条规则纯函数（WorkflowIR[, env] -> list[Suggestion]）。
    analyzer.py   — 汇聚所有规则、按严重度排序，吐最终建议列表。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
