"""routing —— 多库/多路检索路由（批次 2.2 ②）：跨多个事实库/索引检索并保留库来源维度。

两种模式：
    (a) 并行多库检索后【跨库融合】（复用既有 RRF 融合栈）——默认扇出全部库。
    (b) 路由模式（按库描述选择目标库）做成缝——默认离线确定性路由（关键词匹配），LLM 路由 opt-in。

隔离继承：每个库的 base 检索器（A 线 NarrativeIndexRetriever）已在各自出口剔除 RESTRICTED，故跨库
融合层恒为各 base 输出的子集/重排，RESTRICTED 绝不出域。provenance 保留库来源维度：每条融合结果都带
library_id。

Submodules:
    multi_index.py — LibraryIndex / MultiIndexRetriever：并行多库检索 + 跨库 RRF 融合（保留库来源）。
    router.py — LibraryRouter 缝 + make_library_router：按库描述选目标库（默认关键词路由，LLM opt-in）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
