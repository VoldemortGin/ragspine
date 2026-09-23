"""The diagram description is a template around verbatim labels; it adds no meaning."""

from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.diagrams.diagram_description import (
    DESCRIPTION_PRODUCER,
    describe_diagram,
    diagram_description_text,
    reading_order,
)
from ragspine.extraction.evidence.objects.typed_ir import DiagramEdge, DiagramIR, DiagramNode

ANCHOR = SourceAnchor("rev-1", "0" * 64, 0, (0.0, 0.0, 240.0, 160.0), "page-top-left-points")


def _node(node_id: str, x0: float, y0: float, label: str, *span_ids: str) -> DiagramNode:
    return DiagramNode(node_id, label, (x0, y0, x0 + 40.0, y0 + 10.0), span_ids)


def _ir(nodes: tuple[DiagramNode, ...], edges: tuple[DiagramEdge, ...] = ()) -> DiagramIR:
    return DiagramIR("object-1", ANCHOR, nodes, edges, ())


def test_reading_order_quantises_rows_then_sorts_left_to_right() -> None:
    right = _node("a", 100.0, 70.0, "RIGHT")
    left = _node("b", 20.0, 71.0, "LEFT")
    below = _node("c", 60.0, 82.0, "BELOW")

    assert tuple(node.node_id for node in reading_order((right, left, below))) == ("b", "a", "c")


def test_reading_order_breaks_exact_ties_by_node_id() -> None:
    second = _node("n2", 20.0, 70.0, "SECOND")
    first = _node("n1", 20.0, 70.0, "FIRST")

    assert tuple(node.node_id for node in reading_order((second, first))) == ("n1", "n2")


def test_english_template_counts_nodes_and_edges() -> None:
    nodes = (_node("n1", 20.0, 70.0, "PLAN"), _node("n2", 150.0, 70.0, "BUILD"))
    single = _ir(nodes, (DiagramEdge("n1", "n2", None, "leads to"),))
    both = _ir(nodes, (DiagramEdge("n1", "n2", None, "a"), DiagramEdge("n2", "n1", None, "b")))

    assert diagram_description_text(_ir(nodes)) == (
        "Diagram with 2 nodes: PLAN; BUILD. No connecting edges."
    )
    assert diagram_description_text(_ir(nodes[:1])) == (
        "Diagram with 1 node: PLAN. No connecting edges."
    )
    assert diagram_description_text(single) == (
        "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."
    )
    assert diagram_description_text(both) == (
        "Diagram with 2 nodes and 2 edges: PLAN; BUILD. PLAN -> BUILD. BUILD -> PLAN."
    )


def test_chinese_template_uses_the_same_structure() -> None:
    nodes = (_node("n1", 20.0, 70.0, "PLAN"), _node("n2", 150.0, 70.0, "BUILD"))
    linked = _ir(nodes, (DiagramEdge("n1", "n2", None, "leads to"),))

    assert diagram_description_text(_ir(nodes), language="zh") == (
        "流程图，2 个节点：PLAN；BUILD。无连线。"  # noqa: RUF001 — fullwidth CJK punctuation is the Chinese template, never its ASCII lookalike
    )
    assert diagram_description_text(linked, language="zh") == (
        "流程图，2 个节点：PLAN；BUILD。PLAN -> BUILD。"  # noqa: RUF001 — fullwidth CJK punctuation is the Chinese template, never its ASCII lookalike
    )


def test_labels_are_copied_unchanged() -> None:
    label = "Foundation: 100%  Digitalised Agency"
    text = diagram_description_text(_ir((_node("n1", 20.0, 70.0, label),)))

    assert label in text


def test_describe_diagram_carries_cited_spans_in_node_order() -> None:
    nodes = (
        _node("n1", 20.0, 70.0, "PLAN", "span-a", "span-b"),
        _node("n2", 150.0, 70.0, "BUILD", "span-b", "span-c"),
    )

    description = describe_diagram(_ir(nodes))

    assert description.source_span_ids == ("span-a", "span-b", "span-c")
    assert description.object_id == "object-1"
    assert description.source == ANCHOR
    assert description.producer == DESCRIPTION_PRODUCER
    assert description.confidence.score is None
    assert description.verification is Verification.VERIFIED
    assert description.text == diagram_description_text(_ir(nodes))
