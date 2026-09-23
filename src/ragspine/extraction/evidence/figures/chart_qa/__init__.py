"""extraction.evidence.figures.chart_qa —— typed ChartQA:只做确定性查值与有序百分点差值(ADR 0022)。

Submodules:
    displayed_evidence.py — 守住显示值 bar port 的字段,原始血缘与角色契约。
    displayed_models.py — 点局部的期间角色与限定范围的显示值查询结果。
    displayed_ports.py — 显示值 bar 的证明是源适配器的职责,永不来自用户输入。
    displayed_service.py — 经独立的只查找受信 port 读取显式 bar 标签。
    evidence.py — 检查精确的限定字段并生成源出现引用。
    models.py — 不可变问题,带引用的源 claim 与独立的计算回执。
    ports.py — 解析钉住的不可变成员并独立重复其证明。
    service.py — 只做确定性查找与有序百分点减法。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
