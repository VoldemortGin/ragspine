"""extraction.evidence.page —— 源绑定的选页处理记录,覆盖检查与区域几何(ADR 0022)。

Submodules:
    column_regions.py — 把页面的区域标题绑定到各自所在的栏。
    geometry.py — 模型渲染区域与规范源几何之间的包含判定(带容差)。
    models.py — 不可变处理记录,把源观测与推理分开。
    ports.py — 源绑定推理的 ports;实现在 adapters。
    service.py — 源绑定页处理的纯变换与覆盖检查。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
