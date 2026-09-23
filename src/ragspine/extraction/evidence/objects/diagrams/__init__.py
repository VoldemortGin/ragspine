"""extraction.evidence.objects.diagrams —— 图示的零模型证明与确定性投影(ADR 0015)。

Submodules:
    diagram_description.py — 已证明 DiagramIR 的确定性自然语言投影;这里不跑模型。
    diagram_models.py — 图示节点与连线的确定性证明记录;这里不做推断。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
