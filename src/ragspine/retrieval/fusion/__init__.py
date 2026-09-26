"""fusion —— W12-B 多路检索融合：OCR→text 文本通道 + ColPali 视觉通道按 RRF 合一（opt-in）。

按排名综合两路候选；同 (doc, page) 两腿命中相加 + 合并（视觉确认 boost，文本命中优先做代表）。
visual=None 时透传文本腿（等价不融合）。隔离从两腿出口继承，复用 W1 rrf_fuse。

Submodules:
    route_fusion.py — FusedRetriever（NarrativeRetriever 形）+ make_fused_retriever + 融合键/RRF 逻辑。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
