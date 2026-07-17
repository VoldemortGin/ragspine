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
    """节点类型无法归一时的公开错误类型（当前编译管线已不再抛出，保留兼容）。

    宽容原则：Dify 平台能导出的工作流即视为合法输入。http-request / 插件等「留钩子」节点、
    类型完全未知的节点、乃至缺失 data.type 的节点，lower 段都归一为 UnsupportedNode、
    生成带 NotImplementedError 的可运行骨架 + warning，不整体失败。本类型作为公开异常
    API 保留（上层可能 import / 捕获），当前管线内没有抛出点。
    """

    code = "dify.unsupported_node"


class CyclicGraph(DifyCompileError):
    """节点图存在环，无法拓扑排序（Dify 工作流应为 DAG）。"""

    code = "dify.cyclic_graph"
