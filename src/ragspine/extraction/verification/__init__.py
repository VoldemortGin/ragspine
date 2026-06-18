"""verification —— 双通道交叉校验：比对两路抽取结果，分歧项送入人工复核队列。

纯逻辑契约，不依赖 Docling。

Submodules:
    dual_channel_verifier.py — 双通道交叉校验（二期 PDF 线）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
