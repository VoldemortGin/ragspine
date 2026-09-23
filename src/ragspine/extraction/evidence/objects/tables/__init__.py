"""extraction.evidence.objects.tables —— 网格即墨迹:表格网格的划线证明与逐字转写(ADR 0014)。

与 ragspine 的 extraction/tables/(TSR 缝)不是一回事。

Submodules:
    table_grid_proof.py — 由页面划线证明原生表格网格;未证明的一律保持 pending。
    table_models.py — 不可变表格网格,把结构不确定与空单元格区分开。
    table_transcription.py — 逐字表格转写:每个单元格必须逐字复现其源出现。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
