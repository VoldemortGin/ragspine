"""P2 IR + topo 验收：节点归一 / VarRef / topo_order / parallel_layers / 环检测 /
iteration 子图 / UnsupportedNode 留钩子。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ragspine.dify.codegen.emitter import generate_code
from ragspine.dify.errors import CyclicGraph, UnsupportedNodeType
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.ir.model import (
    IfElseNode,
    IterationNode,
    LLMNode,
    StartNode,
    TemplateValue,
    UnsupportedNode,
    VarRef,
    WorkflowIR,
)
from ragspine.dify.ir.topo import parallel_layers, topo_order
from ragspine.dify.parse.loader import parse_dify_yaml


def _ir(fixture_text: Callable[[str], str], name: str) -> WorkflowIR:
    return lower_to_ir(parse_dify_yaml(fixture_text(name)))


# ---- 节点归一 -------------------------------------------------------------


def test_node_kinds_normalized(fixture_text: Callable[[str], str]) -> None:
    ir = _ir(fixture_text, "seq")
    kinds = {n.id: n.kind for n in ir.graph.nodes}
    assert kinds == {
        "start_1": "start", "llm_1": "llm",
        "tt_1": "template-transform", "end_1": "end",
    }
    assert isinstance(ir.node("start_1"), StartNode)
    assert isinstance(ir.node("llm_1"), LLMNode)


def test_llm_node_fields(fixture_text: Callable[[str], str]) -> None:
    llm = ir_node(_ir(fixture_text, "seq"), "llm_1")
    assert isinstance(llm, LLMNode)
    assert llm.max_tokens == 1024
    assert llm.model_name == "claude-opus-4-8"
    assert len(llm.messages) == 2
    assert llm.messages[0].role == "system"
    assert llm.messages[1].role == "user"


def test_start_variables(fixture_text: Callable[[str], str]) -> None:
    start = ir_node(_ir(fixture_text, "seq"), "start_1")
    assert isinstance(start, StartNode)
    assert start.variables == ("question",)


# ---- VarRef / 模板归一 ----------------------------------------------------


def test_varref_from_template(fixture_text: Callable[[str], str]) -> None:
    """LLM user 消息里的 {{#start_1.question#}} → VarRef(start_1, question)。"""
    llm = ir_node(_ir(fixture_text, "seq"), "llm_1")
    assert isinstance(llm, LLMNode)
    user_tpl = llm.messages[1].text
    assert isinstance(user_tpl, TemplateValue)
    assert VarRef("start_1", "question") in user_tpl.refs()
    assert llm.dep_refs() == (VarRef("start_1", "question"),)


def test_varref_from_value_selector(fixture_text: Callable[[str], str]) -> None:
    """end 节点 value_selector [tt_1, output] → VarRef(tt_1, output)。"""
    end = ir_node(_ir(fixture_text, "seq"), "end_1")
    assert end.dep_refs() == (VarRef("tt_1", "output"),)


def test_template_transform_jinja_mapping(fixture_text: Callable[[str], str]) -> None:
    """template-transform 的 Jinja {{ text }} 按 variables 表映射到 VarRef(llm_1, text)。"""
    tt = ir_node(_ir(fixture_text, "seq"), "tt_1")
    assert tt.dep_refs() == (VarRef("llm_1", "text"),)


# ---- topo / 并行分层 ------------------------------------------------------


def test_topo_and_layers_seq(fixture_text: Callable[[str], str]) -> None:
    ir = _ir(fixture_text, "seq")
    assert ir.topo_order == ("start_1", "llm_1", "tt_1", "end_1")
    assert ir.parallel_layers == (("start_1",), ("llm_1",), ("tt_1",), ("end_1",))


def test_parallel_layer_groups_independent_nodes(
    fixture_text: Callable[[str], str],
) -> None:
    """parallel fixture：llm_a 与 llm_b 互不依赖 → 同处一个 parallel_layer。"""
    ir = _ir(fixture_text, "parallel")
    layer_with_llms = next(
        layer for layer in ir.parallel_layers if "llm_a" in layer
    )
    assert set(layer_with_llms) == {"llm_a", "llm_b"}


def test_branch_ifelse_condition_rendered(fixture_text: Callable[[str], str]) -> None:
    ifelse = ir_node(_ir(fixture_text, "branch"), "ifelse_1")
    assert isinstance(ifelse, IfElseNode)
    assert len(ifelse.branches) == 1
    branch = ifelse.branches[0]
    assert branch.handle == "true"
    assert branch.condition_expr is not None
    assert ">" in branch.condition_expr
    assert VarRef("start_1", "score") in branch.refs


def test_topo_order_deterministic() -> None:
    """同一组节点/边恒定产出同一拓扑序（按 id 打破平局）。"""
    nodes = ["c", "a", "b"]
    edges = [("a", "b"), ("a", "c")]
    assert topo_order(nodes, edges) == ("a", "b", "c")
    assert parallel_layers(nodes, edges) == (("a",), ("b", "c"))


# ---- 环检测 ---------------------------------------------------------------


def test_cycle_detected() -> None:
    nodes = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c"), ("c", "a")]
    with pytest.raises(CyclicGraph) as ei:
        topo_order(nodes, edges)
    assert ei.value.code == "dify.cyclic_graph"
    assert set(ei.value.context.get("nodes", [])) == {"a", "b", "c"}
    with pytest.raises(CyclicGraph):
        parallel_layers(nodes, edges)


# ---- iteration 子图 -------------------------------------------------------


def test_iteration_subgraph(fixture_text: Callable[[str], str]) -> None:
    ir = _ir(fixture_text, "iteration")
    iter_node = ir_node(ir, "iter_1")
    assert isinstance(iter_node, IterationNode)
    assert iter_node.is_parallel is True
    assert iter_node.parallel_nums == 5
    assert iter_node.iterator == VarRef("start_1", "items")
    # 内层 llm 节点被剥离进子图，不出现在主图。
    assert "iter_llm" not in {n.id for n in ir.graph.nodes}
    assert iter_node.body is not None
    assert {n.id for n in iter_node.body.graph.nodes} == {"iter_llm"}


def test_dify_iteration_start_is_structural_not_executable() -> None:
    """Dify 0.6 的 iteration-start 画布锚点不进入 IR，也不生成 unsupported 钩子。"""
    dsl = """
app:
  mode: workflow
  name: current-dify-iteration
version: "0.6.0"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: Start
          variables:
            - {variable: items, type: "array[string]", required: true}
      - id: iteration_1
        data:
          type: iteration
          title: Iterate
          iterator_selector: [start_1, items]
          output_selector: [item_llm, text]
          output_type: "array[string]"
          start_node_id: iteration_start_1
      - id: iteration_start_1
        parentId: iteration_1
        type: custom-iteration-start
        data:
          type: iteration-start
          title: ""
          isInIteration: true
      - id: item_llm
        parentId: iteration_1
        data:
          type: llm
          title: Process item
          iteration_id: iteration_1
          model: {provider: langgenius/openai/openai, name: gpt-4o-mini, mode: chat}
          prompt_template:
            - {role: user, text: "Item: {{#iteration_1.item#}}"}
      - id: end_1
        data:
          type: end
          title: End
          outputs:
            - {variable: results, value_selector: [iteration_1, output]}
    edges:
      - {source: start_1, target: iteration_1, sourceHandle: source}
      - {source: iteration_start_1, target: item_llm, sourceHandle: source}
      - {source: iteration_1, target: end_1, sourceHandle: source}
"""
    ir = lower_to_ir(parse_dify_yaml(dsl))
    iteration = ir_node(ir, "iteration_1")
    assert isinstance(iteration, IterationNode)
    assert {node.id for node in ir.graph.nodes} == {"start_1", "iteration_1", "end_1"}
    assert {(edge.source, edge.target) for edge in ir.graph.edges} == {
        ("start_1", "iteration_1"),
        ("iteration_1", "end_1"),
    }
    assert iteration.body is not None
    assert {node.id for node in iteration.body.graph.nodes} == {"item_llm"}
    assert iteration.body.graph.edges == ()
    assert generate_code(ir).warnings == ()


# ---- UnsupportedNode 留钩子（不抛异常） -----------------------------------


def test_unsupported_node_becomes_hook() -> None:
    """http-request 等未建模类型 → UnsupportedNode（留钩子），不抛 UnsupportedNodeType。"""
    dsl = """
app:
  mode: workflow
  name: hook-demo
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: []}
      - id: http_1
        data:
          type: http-request
          title: 调外部API
          method: get
          url: https://example.com
      - id: end_1
        data: {type: end, title: 结束, outputs: []}
    edges:
      - {source: start_1, target: http_1, sourceHandle: source}
      - {source: http_1, target: end_1, sourceHandle: source}
"""
    ir = lower_to_ir(parse_dify_yaml(dsl))
    hook = ir_node(ir, "http_1")
    assert isinstance(hook, UnsupportedNode)
    assert hook.node_type == "http-request"
    assert hook.kind == "http-request"


def test_node_missing_type_raises() -> None:
    """节点完全缺 data.type → UnsupportedNodeType（无法归一，硬错）。"""
    dsl = """
app:
  mode: workflow
workflow:
  graph:
    nodes:
      - id: mystery
        data: {title: 无类型}
    edges: []
"""
    with pytest.raises(UnsupportedNodeType):
        lower_to_ir(parse_dify_yaml(dsl))


def ir_node(ir: WorkflowIR, node_id: str):  # type: ignore[no-untyped-def]
    """便捷：按 id 取节点。"""
    return ir.node(node_id)
