"""extraction.evidence.figures —— 源绑定的图表值与注入式用例:同一 SVG 双分支,快照绑定(ADR 0022)。

Submodules:
    chart_qa/ — typed ChartQA:限定字段的查值,引用与百分点差值,以及显示值查找。
    models.py — SVG 分支,配对与快照解析共享的不可变值。
    ports.py — I/O 契约;具体的存储,模型与向量 SDK 在 adapters。
    service.py — 显式注入分支与存储依赖的图表用例门面。
    source_label_match.py — 用相邻源文本观测的短窗口匹配图表字符串(ADR 0016)。
    validation.py — 显式标签切片的保守证据与 claim 校验。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
