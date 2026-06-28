"""visual —— W12 ColPali 视觉文档检索：页作为图像 + patch 级晚交互，无 OCR→text（opt-in，最重）。

GPU + 视觉语言模型，默认关、绝不在精简/CPU 默认路径上；与 W3a 家族 OCR→text 并存（图表密集胜视觉，
离线/确定/CPU 胜 OCR→text）。复用 W11 多向量 MaxSim（patch 级而非 text token 级）。

Submodules:
    colpali.py — VisualMultiVectorBackend 缝 + PageImage + ColPaliRetriever（patch MaxSim，RESTRICTED 出口剔除）
        + FastEmbedColPaliBackend（fastembed LateInteractionMultimodalEmbedding，[colpali]）+ make_colpali_retriever。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
