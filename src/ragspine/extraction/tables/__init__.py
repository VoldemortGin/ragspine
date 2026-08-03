"""表格域：表格结构识别缝（已知表格区域 → 单元格网格）。

检测（"这块是不是表"）不在本域——pdfspine `find_tables(strategy="text")` 实测召回 79.6%、
精确率 100%，够用；崩掉的是检出之后的网格重建（GriTS_Top 0.233），那才是本域要解决的。

Submodules:
    structure.py — TableStructureRecognizer 缝：Protocol + 确定性默认实现 + 选型工厂。
    adapters/ — 真实后端薄适配器（视觉模型等），经可选 extra 延迟 import。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
