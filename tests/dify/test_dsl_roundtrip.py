"""DSL-001：固定兼容 DSL 版本下的 import → IR →（DSL 文档）export → import 语义契约。

现状说明（如实）：编译器没有 IR → Dify YAML 的反向导出器；仓库里现成的 Dify YAML 导出是
`workflows.formats.dump_dify_yaml`（DSL 文档级、稳定无 alias 的序列化）。本文件因此验证：

1. 导入确定性：同一 fixture 两次 import → IR 完全相等；
2. 文档级往返：fixture → parse_workflow → dump_dify_yaml → 再 import，IR 与直接 import 语义等价
   （节点集合、边、拓扑序、关键字段），未知节点的原始配置不丢；
3. 显式诊断：未支持节点 / 缺类型 / 未支持的条件算子一律落 UnsupportedNode + 编译 warning，
   生成代码运行到此抛 NotImplementedError——绝不静默降级成别的语义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.ir.model import (
    IfElseNode,
    IterationNode,
    LoopNode,
    UnsupportedNode,
    WorkflowIR,
)
from ragspine.dify.parse.loader import parse_dify_yaml
from ragspine.workflows.formats import dump_dify_yaml, parse_workflow

# 本仓 fixture 固定的 DSL `version:` 值（手写合成 fixture；与上游当前 DSL 版本的关系见
# docs/compat-dify.md，未经验证的上游版本一律「未验证」）。
PINNED_FIXTURE_DSL_VERSION = "0.1.5"
FIXTURE_NAMES = ("agent_tool", "branch", "iteration", "knowledge", "parallel", "qa_fold", "seq")


def _ir(text: str) -> WorkflowIR:
    return lower_to_ir(parse_dify_yaml(text))


def _export(text: str) -> str:
    return dump_dify_yaml(parse_workflow(text, format="yaml"))


def _semantics(ir: WorkflowIR) -> tuple[Any, ...]:
    """语义摘要：模式、节点 (id, kind)、边、拓扑序；容器节点递归其子图。"""
    nodes: list[tuple[Any, ...]] = []
    for node in ir.graph.nodes:
        body = node.body if isinstance(node, IterationNode | LoopNode) else None
        nodes.append((node.id, node.kind, _semantics(body) if body is not None else None))
    edges = sorted((e.source, e.target, e.source_handle or "") for e in ir.graph.edges)
    return (ir.mode, tuple(sorted(nodes)), tuple(edges), ir.topo_order)


# ---------------------------------------------------------------------------
# 固定的兼容 DSL 版本
# ---------------------------------------------------------------------------


def test_fixture_set_and_dsl_version_are_pinned(fixtures_dir: Path) -> None:
    assert tuple(sorted(p.stem for p in fixtures_dir.glob("*.yml"))) == FIXTURE_NAMES
    for name in FIXTURE_NAMES:
        doc = parse_workflow((fixtures_dir / f"{name}.yml").read_bytes(), format="yaml")
        assert doc.get("kind") == "app", name
        assert doc.get("version") == PINNED_FIXTURE_DSL_VERSION, name


# ---------------------------------------------------------------------------
# import 确定性 + 文档级 export 往返
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_import_is_deterministic(name: str, fixture_text: Any) -> None:
    text = fixture_text(name)
    assert _ir(text) == _ir(text)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_import_export_import_preserves_semantics(name: str, fixture_text: Any) -> None:
    text = fixture_text(name)
    first = _ir(text)
    exported = _export(text)
    second = _ir(exported)

    assert _semantics(second) == _semantics(first)
    assert second == first  # 关键字段（模板、条件、模型、参数等）逐字段相等
    # 导出物仍是带固定版本头的 Dify DSL，且再导出是不动点（稳定、确定）。
    reparsed = parse_workflow(exported, format="yaml")
    assert reparsed["kind"] == "app"
    assert reparsed["version"] == PINNED_FIXTURE_DSL_VERSION
    assert _export(exported) == exported


# ---------------------------------------------------------------------------
# 未支持节点 / 字段的显式诊断
# ---------------------------------------------------------------------------

_UNKNOWN_NODE = """
app: {mode: workflow, name: unknown-node}
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: odd_1
        data: {type: list-operator, title: 列表操作, filter_by: {enabled: true}, extra_x: 7}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [odd_1, result]}]}
    edges:
      - {source: start_1, target: odd_1}
      - {source: odd_1, target: end_1}
"""

_MISSING_TYPE = """
app: {mode: workflow, name: missing-type}
workflow:
  graph:
    nodes:
      - id: blank_1
        data: {title: 无类型}
    edges: []
"""


def _if_else_dsl(op: str) -> str:
    return f"""
app: {{mode: workflow, name: op-{op!r}}}
workflow:
  graph:
    nodes:
      - id: start_1
        data: {{type: start, title: 开始, variables: [{{variable: x, type: text-input}}]}}
      - id: if_1
        data:
          type: if-else
          title: 判断
          cases:
            - case_id: "true"
              logical_operator: and
              conditions:
                - {{variable_selector: [start_1, x], comparison_operator: "{op}", value: "a"}}
      - id: end_1
        data: {{type: end, title: 结束, outputs: []}}
    edges:
      - {{source: start_1, target: if_1}}
      - {{source: if_1, target: end_1, sourceHandle: "true"}}
"""


def _loop_dsl(op: str) -> str:
    return f"""
app: {{mode: workflow, name: loop-op}}
workflow:
  graph:
    nodes:
      - id: loop_1
        data:
          type: loop
          title: 循环
          loop_count: 3
          logical_operator: and
          break_conditions:
            - {{variable_selector: [loop_1, i], comparison_operator: "{op}", value: "3"}}
          loop_variables: [{{label: i, value_type: constant, value: 0}}]
    edges: []
"""


def test_unknown_node_is_explicitly_diagnosed_and_survives_roundtrip() -> None:
    node = _ir(_UNKNOWN_NODE).node("odd_1")
    assert isinstance(node, UnsupportedNode)
    assert node.node_type == "list-operator"
    assert dict(node.raw)["extra_x"] == 7  # 未知字段原样保留，不静默丢弃

    result = compile_dify_yaml(_UNKNOWN_NODE, analyze=False)
    assert any("odd_1" in w and "list-operator" in w for w in result.code.warnings)

    # import → export → import 后诊断与原始配置仍在（不因往返被「洗白」）。
    again = _ir(_export(_UNKNOWN_NODE)).node("odd_1")
    assert again == node


def test_unknown_node_refuses_to_run_instead_of_silently_skipping() -> None:
    code = compile_dify_yaml(_UNKNOWN_NODE, analyze=False).code
    ns: dict[str, Any] = {}
    exec(compile(code.source, "<dsl-001>", "exec"), ns)  # noqa: S102
    with pytest.raises(NotImplementedError, match="odd_1"):
        ns["run_workflow"](ns["Inputs"](q="hi"))


def test_missing_node_type_is_explicitly_diagnosed() -> None:
    node = _ir(_MISSING_TYPE).node("blank_1")
    assert isinstance(node, UnsupportedNode)
    assert node.node_type == ""
    warnings = compile_dify_yaml(_MISSING_TYPE, analyze=False).code.warnings
    assert any("blank_1" in w and "缺少节点类型" in w for w in warnings)


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ("=", "== 'a'"),
        ("≠", "!= 'a'"),  # Dify 上游「不等于」符号；绝不能被当成相等
        ("is", "== 'a'"),
        ("is not", "!= 'a'"),
    ],
)
def test_upstream_equality_operators_keep_semantics(op: str, expected: str) -> None:
    node = _ir(_if_else_dsl(op)).node("if_1")
    assert isinstance(node, IfElseNode)
    expr = node.branches[0].condition_expr
    assert expr is not None and expr.endswith(expected)


@pytest.mark.parametrize(
    "op",
    ["in", "not in", "all of", "null", "not null", "exists", "not exists", "shuffle"],
)
def test_unsupported_comparison_operator_is_diagnosed_not_degraded(op: str) -> None:
    dsl = _if_else_dsl(op)
    node = _ir(dsl).node("if_1")
    assert isinstance(node, UnsupportedNode)
    assert node.node_type == "if-else"
    warnings = compile_dify_yaml(dsl, analyze=False).code.warnings
    assert any("if_1" in w and "if-else" in w for w in warnings)


def test_unsupported_loop_break_operator_is_diagnosed_not_degraded() -> None:
    node = _ir(_loop_dsl("exists")).node("loop_1")
    assert isinstance(node, UnsupportedNode)
    assert node.node_type == "loop"
    assert isinstance(_ir(_loop_dsl("≥")).node("loop_1"), LoopNode)
