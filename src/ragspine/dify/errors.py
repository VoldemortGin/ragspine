"""Dify 编译器域的统一异常（继承家族 corespine.CorespineError，带稳定可 grep 的 code）。

三段管线（parse → IR → codegen/optimize）共用这一组异常：边界校验失败、遇到尚不支持的
节点类型 / app 模式、节点图成环。都带机器可判别的 `code`（如 "dify.unsupported_node"），
便于上层统一捕获与日志归一（corespine.error_to_dict）。具体哪些节点/模式不支持由各段绑，
本模块只给稳定的异常类型（机制，非保证；ADR 0001 D6 / ragspine ADR 0013）。
"""

from __future__ import annotations

from corespine import CorespineError


class DifyCompileError(CorespineError):
    """Dify 工作流编译期错误的统一基类（parse / lower / codegen 通用）。"""

    code = "dify.compile"


class UnsupportedAppMode(DifyCompileError):
    """app.mode 不在已支持集合（当前支持 workflow / advanced-chat）。"""

    code = "dify.unsupported_app_mode"


class UnsupportedNodeType(DifyCompileError):
    """遇到尚未建模的节点类型（lower 段无法归一到任何 IRNode 子类）。

    注意：http-request / tool / knowledge-retrieval 等「留钩子」的节点【不】走此异常——
    它们被显式建模为 UnsupportedNode、生成带 NotImplementedError 的骨架函数 + warning，
    产出可运行骨架而非整体失败。本异常只在节点类型【完全未知 / 无法归一】时抛出。
    """

    code = "dify.unsupported_node"


class CyclicGraph(DifyCompileError):
    """节点图存在环，无法拓扑排序（Dify 工作流应为 DAG）。"""

    code = "dify.cyclic_graph"
