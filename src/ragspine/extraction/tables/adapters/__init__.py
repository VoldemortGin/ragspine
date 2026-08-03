"""TableStructureRecognizer 的真实后端适配器（可选 extra，延迟 import）。

Submodules:
    tatr.py — Table Transformer（DETR 架构视觉 TSR 模型）适配器，需 [tsr] extra。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
