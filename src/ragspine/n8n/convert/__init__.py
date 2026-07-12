"""convert 段：n8n ↔ dify 双向转换核心（纯 stdlib，零 pydantic）。

Submodules:
    mapping.py   — 数据驱动的节点/算子/输出字段映射表（纯常量，集中一处）。
    variables.py — 变量表达式双向转换（{{ $json.x }} ↔ {{#id.x#}}，转不动原样保留 + warning）。
    to_dify.py   — n8n → dify（attachment 归并 / noOp splice / 合成 start-end / _n8n 埋点）。
    to_n8n.py    — dify → n8n（_n8n 无损还原 / 映射表新建 / connections 反向构建）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
