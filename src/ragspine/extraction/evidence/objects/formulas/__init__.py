"""extraction.evidence.objects.formulas —— 公式的零模型证明(ADR 0015)。

Submodules:
    formula_models.py — 源证明的公式 token:每个 token 引用一段 span,每个结构引用一条 path。
    formula_rules.py — 仅凭自身 span 与 path 证明公式:纯,确定,可回放,失败即关闭。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
