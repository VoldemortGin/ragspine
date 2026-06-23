"""ir 段：DifyDoc → WorkflowIR（去 Dify 化的节点图 + 数据流 + 拓扑 + parallel_layers）。

IR 是三段的解耦中枢：节点归一为各 IRNode 子类（frozen dataclass）、变量引用归一为
VarRef/Literal/TemplateValue，边带 source_handle，拓扑给出 topo_order 与 parallel_layers。
纯 stdlib，零 pydantic、零 Dify 概念泄漏到此层以下。

Submodules:
    model.py — VarRef/Literal/TemplateValue + IRNode 子类 + IREdge/IRGraph/WorkflowIR。
    lower.py — DifyDoc → WorkflowIR：节点/变量引用归一、拓扑、iteration 子图。
    topo.py  — Kahn 拓扑排序 + parallel_layers 分层 + 环检测（CyclicGraph）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
