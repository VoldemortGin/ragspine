"""filtering —— 元数据过滤（批次 2.2 ①）：检索管线里【打分之前】的确定性条件过滤。

manual 先行：显式条件（=、in、范围等最小算子集）确定性收窄候选；automatic（LLM 从 query 抽过滤
条件）做成 opt-in 缝——默认离线路径不启用，抽取结果只作过滤条件、绝不进答案通道。过滤只【收窄】
候选（结果恒为输入子集），故 RESTRICTED 语义绝不被过滤器绕过（link/rerank 双出口照常剔除）。

Submodules:
    metadata_filter.py — FilterCondition / MetadataFilter：确定性、零依赖的条件过滤（收窄候选）。
    automatic.py — FilterExtractor 缝 + make_filter_extractor：从 query 抽过滤条件（默认关，opt-in）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
