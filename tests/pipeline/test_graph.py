"""PipelineGraph 值类型与导出器的红色测试（TDD，Phase 1）。

只验证对外行为：在一张手搭的 PipelineGraph 上断言三种导出器的输出形态
（Mermaid 渲染为 flowchart；DOT 渲染为 digraph；dict 经 json 往返）、merge
的并集语义（节点按 id 去重、边保留）、以及确定性（同图导出两次逐字节一致）。

覆盖 PRD 测试类目 1（exporters on a known graph）、5（merge 并集/去重/边保留）、
8（determinism）。

红色预期：因 `from ragspine.pipeline.graph import ...` ImportError（graph.py 尚未
实现）而 FAIL。import 放在每个测试体内首行，使其作为用例 FAILURE（而非 collection
ERROR）暴露——沿用本仓库其他红色阶段测试的约定。

每个用例 docstring 带中文 user story。
"""

import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


def _sample_graph():
    """构造一张覆盖各 kind/edge 形态的手搭图（含需转义的特殊字符）。"""
    from ragspine.pipeline.graph import Edge, Node, PipelineGraph

    nodes = (
        Node(id="parse", label="parse_intent", kind="stage", domain="agent"),
        Node(id="gate", label='security "gate"', kind="gate", domain="agent"),
        Node(id="store", label="FactStore", kind="store", domain="storage"),
        Node(id="route", label="route?", kind="stage", domain="agent"),
    )
    edges = (
        Edge(src="parse", dst="gate"),
        Edge(src="gate", dst="route", label="answer"),
        Edge(src="route", dst="store", label="route=structured", kind="conditional"),
    )
    return PipelineGraph(title="Sample Pipeline", nodes=nodes, edges=edges)


def test_to_mermaid_renders_flowchart():
    """作为开发者，我要 to_mermaid() 产出可粘贴进 README 的 Mermaid flowchart。"""
    from ragspine.pipeline.graph import PipelineGraph  # noqa: F401

    g = _sample_graph()
    out = g.to_mermaid()

    # 头部声明为 flowchart，默认方向 TD。
    assert out.splitlines()[0].strip() == "flowchart TD"
    # 节点以 id["label"] 形态出现；含引号的 label 被安全转义（不破坏语法）。
    assert 'parse["parse_intent"]' in out
    assert "FactStore" in out
    # gate 节点用菱形/特殊形状（kind=gate），与普通 stage 区分。
    assert "gate{" in out
    # 边：无 label 为 -->；有 label 为 -->|label|。
    assert "parse --> gate" in out
    assert "gate -->|answer| route" in out
    assert "route -->|route=structured| store" in out
    # 含双引号的 label 不应原样泄漏破坏 mermaid 语法。
    assert 'security "gate"' not in out


def test_to_mermaid_direction_override():
    """作为开发者，我要能切换布局方向（如 LR）以适配不同文档。"""
    from ragspine.pipeline.graph import PipelineGraph  # noqa: F401

    g = _sample_graph()
    assert g.to_mermaid(direction="LR").splitlines()[0].strip() == "flowchart LR"


def test_to_dot_renders_digraph():
    """作为开发者，我要 to_dot() 产出可交给 Graphviz 离线渲染的 digraph。"""
    from ragspine.pipeline.graph import PipelineGraph  # noqa: F401

    g = _sample_graph()
    out = g.to_dot()

    assert out.lstrip().startswith("digraph")
    assert out.rstrip().endswith("}")
    # 节点：\"id\" [label=\"...\"]。
    assert '"parse" [label="parse_intent"]' in out
    # 边：\"src\" -> \"dst\" [label=\"...\"]。
    assert '"parse" -> "gate"' in out
    assert '"gate" -> "route" [label="answer"]' in out
    assert '"route" -> "store" [label="route=structured"]' in out


def test_to_dict_round_trips_through_json():
    """作为开发者，我要 to_dict() 的 JSON 能往返，以喂给我自己的 UI / 工具。"""
    from ragspine.pipeline.graph import PipelineGraph  # noqa: F401

    g = _sample_graph()
    d = g.to_dict()

    # 顶层结构。
    assert d["title"] == "Sample Pipeline"
    assert isinstance(d["nodes"], list)
    assert isinstance(d["edges"], list)
    assert len(d["nodes"]) == 4
    assert len(d["edges"]) == 3

    # 节点/边字段完整。
    first_node = d["nodes"][0]
    assert first_node["id"] == "parse"
    assert first_node["label"] == "parse_intent"
    assert first_node["kind"] == "stage"
    assert first_node["domain"] == "agent"
    assert first_node["symbol"] is None

    cond_edge = d["edges"][2]
    assert cond_edge["src"] == "route"
    assert cond_edge["dst"] == "store"
    assert cond_edge["label"] == "route=structured"
    assert cond_edge["kind"] == "conditional"

    # 往返：dumps → loads 等价。
    assert json.loads(json.dumps(d)) == d


def test_merge_unions_nodes_dedup_by_id_and_preserves_edges():
    """作为维护者，我要 merge() 把两张子图并起来：节点按 id 去重、边全保留。"""
    from ragspine.pipeline.graph import Edge, Node, PipelineGraph

    a = PipelineGraph(
        title="A",
        nodes=(
            Node(id="x", label="X", kind="stage"),
            Node(id="y", label="Y", kind="stage"),
        ),
        edges=(Edge(src="x", dst="y"),),
    )
    b = PipelineGraph(
        title="B",
        nodes=(
            Node(id="y", label="Y-from-B", kind="store"),  # 与 a 的 y 撞 id → first wins
            Node(id="z", label="Z", kind="stage"),
        ),
        edges=(Edge(src="y", dst="z"),),
    )

    merged = a.merge(b)

    # 标题取 self。
    assert merged.title == "A"
    # 节点并集、按 id 去重、self 优先（y 保留 a 的版本）。
    ids = [n.id for n in merged.nodes]
    assert ids == ["x", "y", "z"]
    y_node = next(n for n in merged.nodes if n.id == "y")
    assert y_node.label == "Y"  # first wins：保留 a 的 y
    assert y_node.kind == "stage"
    # 边全保留（self 的在前，other 的在后）。
    assert merged.edges == (Edge(src="x", dst="y"), Edge(src="y", dst="z"))


def test_merge_group_tags_other_nodes_domain():
    """作为维护者，我要 merge(group=...) 给 other 的【新】节点打上 domain 分组标签。"""
    from ragspine.pipeline.graph import Edge, Node, PipelineGraph

    a = PipelineGraph(title="A", nodes=(Node(id="x", label="X", kind="stage"),), edges=())
    b = PipelineGraph(
        title="B",
        nodes=(
            Node(id="x", label="X", kind="stage"),  # 撞 id：不被引入，故不被打标
            Node(id="z", label="Z", kind="stage", domain="orig"),
        ),
        edges=(Edge(src="x", dst="z"),),
    )

    merged = a.merge(b, group="retrieval")

    # 只有 other 真正【新加入】的节点（z）被打上分组 domain。
    z_node = next(n for n in merged.nodes if n.id == "z")
    assert z_node.domain == "retrieval"
    # self 的 x 不受影响（domain 仍为 None）。
    x_node = next(n for n in merged.nodes if n.id == "x")
    assert x_node.domain is None


def test_exports_are_byte_identical_across_calls():
    """作为贡献者，我要导出确定（同图两次逐字节一致），让重生成的 diff 干净可审。"""
    from ragspine.pipeline.graph import PipelineGraph  # noqa: F401

    g = _sample_graph()
    assert g.to_mermaid() == g.to_mermaid()
    assert g.to_dot() == g.to_dot()
    assert json.dumps(g.to_dict()) == json.dumps(g.to_dict())

    # 两张内容相同、独立构造的图，导出也必须一致（顺序按声明，无集合乱序）。
    assert _sample_graph().to_mermaid() == g.to_mermaid()
    assert _sample_graph().to_dot() == g.to_dot()
