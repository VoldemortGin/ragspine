"""extraction.evidence.objects —— 版面对象的 typed payload 与零模型证明(ADR 0014/0015)。

Submodules:
    diagrams/ — 图示节点与连线的确定性证明记录及其模板化描述(ADR 0015)。
    formulas/ — 公式 token 与结构的源证明规则(ADR 0015)。
    tables/ — 划线网格证明与逐字表格转写(ADR 0014)。
    typed_ir.py — 对象 typed payload;推理出的结构永不覆盖源观测。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
