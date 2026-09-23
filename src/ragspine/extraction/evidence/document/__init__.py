"""extraction.evidence.document —— 源观测值与源身份,不依赖基础设施 SDK(ADR 0022)。

Submodules:
    models.py — 不可变的源观测值;语义资格化刻意留待后续。
    ports.py — 源抽取与内容寻址持久化的基础设施契约。
    service.py — 先校验源身份,再调用 SDK 或持久化。
    text_layer.py — 单页文本层质量:只做诊断,不修复任何页面。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
