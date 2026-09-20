"""Every diagram rule has a worked example and a refusal; a failure stops the whole object."""

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.diagram_qualification import (
    DiagramQualificationError,
    qualify_diagram,
)
from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import SourceAnchor, Verification
from enterprise_pdf_rag.processing.diagram_description import DESCRIPTION_PRODUCER
from enterprise_pdf_rag.processing.diagram_models import (
    DIAGRAM_METHOD,
    DIAGRAM_SCOPE,
    DiagramQualification,
)
from enterprise_pdf_rag.processing.typed_ir import DiagramEdge, DiagramIR, DiagramNode
from tests.enterprise_pdf_rag.adapters.test_diagram_geometry import (
    OBJECT_BBOX,
    crop_svg,
    line_path,
    rect_path,
    rounded_rect_path,
    triangle_path,
)

MANIFEST = "m" * 64
ANCHOR = SourceAnchor("rev-1", "0" * 64, 0, OBJECT_BBOX, "page-top-left-points")
N1: Bounds = (20.0, 70.0, 90.0, 100.0)
N2: Bounds = (150.0, 70.0, 220.0, 100.0)
LINE = ((90.0, 85.0), (142.0, 85.0))
ARROW = ((142.0, 81.0), (142.0, 89.0), (150.0, 85.0))
PLAN = TextSpan("span-plan", "PLAN", (28.0, 81.0, 52.0, 91.0))
BUILD = TextSpan("span-build", "BUILD", (158.0, 81.0, 188.0, 91.0))


def _node(node_id: str, bbox: Bounds, label: str, *span_ids: str) -> DiagramNode:
    return DiagramNode(node_id, label, bbox, span_ids)


def _edge(
    source: str, target: str, *, label: str | None = None, span_ids: tuple[str, ...] = ()
) -> DiagramEdge:
    return DiagramEdge(source, target, label, "leads to", Verification.PENDING, span_ids)


def _ir(
    nodes: tuple[DiagramNode, ...],
    edges: tuple[DiagramEdge, ...] = (),
    *,
    anchor: SourceAnchor = ANCHOR,
) -> DiagramIR:
    return DiagramIR("object-1", anchor, nodes, edges, ("model diagnostic",))


def _two_nodes(*extra: str) -> bytes:
    return crop_svg(
        rect_path(N1),
        rect_path(N2),
        line_path(*LINE),
        triangle_path(*ARROW),
        triangle_path(*ARROW, fill="none", stroke="#000000"),
        *extra,
    ).encode()


def test_two_nodes_one_arrow_qualify() -> None:
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)),
        (_edge("n1", "n2"),),
    )

    qualified = qualify_diagram(
        svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST
    )

    edge = qualified.qualification.edges[0]
    assert (edge.tip, edge.base_mid, edge.line_end_at_target) == (
        (150.0, 85.0),
        (142.0, 85.0),
        (142.0, 85.0),
    )
    assert edge.arrowhead.path_index != edge.line.path_index
    assert tuple(node.node_id for node in qualified.qualification.nodes) == ("n1", "n2")
    assert qualified.qualification.nodes[0].clipped_bounds == N1
    assert qualified.qualification.source_span_ids == ("span-plan", "span-build")
    assert qualified.qualification.scope == DIAGRAM_SCOPE
    assert qualified.qualification.source_manifest_id == MANIFEST
    assert (
        qualified.description.text == "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."
    )
    assert qualified.description.producer == DESCRIPTION_PRODUCER
    assert qualified.description.verification is Verification.VERIFIED
    assert qualified.ir.verification is Verification.VERIFIED
    assert qualified.ir.edges[0].verification is Verification.VERIFIED
    assert qualified.ir.nodes == ir.nodes
    assert qualified.ir.diagnostics == (
        "model diagnostic",
        f"Structure proven independently: {DIAGRAM_METHOD}",
    )


def test_nodes_only_diagram_qualifies_without_edges() -> None:
    bbox: Bounds = (15.0, 65.0, 210.0, 140.0)
    anchor = SourceAnchor("rev-1", "0" * 64, 0, bbox, "page-top-left-points")
    frames: tuple[Bounds, ...] = (
        (20.0, 70.0, 200.0, 88.0),
        (20.0, 94.0, 200.0, 112.0),
        (20.0, 118.0, 200.0, 136.0),
    )
    labels = ("ALPHA", "BETA", "GAMMA")
    spans = tuple(
        TextSpan(f"span-{label.lower()}", label, (30.0, frame[1] + 4, 90.0, frame[1] + 14))
        for label, frame in zip(labels, frames, strict=True)
    )
    svg = crop_svg(
        rect_path((0.0, 0.0, 240.0, 160.0), fill="#ffffff", stroke="none"),
        *(rounded_rect_path(frame, 4.8) for frame in frames),
        bbox=bbox,
    ).encode()
    ir = _ir(
        tuple(
            _node(f"node-{label.lower()}", frame, label, span.span_id)
            for label, frame, span in zip(labels, frames, spans, strict=True)
        ),
        anchor=anchor,
    )

    qualified = qualify_diagram(svg=svg, spans=spans, ir=ir, source_manifest_id=MANIFEST)

    assert qualified.qualification.edges == ()
    assert qualified.description.text == (
        "Diagram with 3 nodes: ALPHA; BETA; GAMMA. No connecting edges."
    )


def test_empty_label_node_rejects_whole_object() -> None:
    ir = _ir((_node("n1", N1, "", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "node n1: empty_label_without_source_occurrence"


def test_node_without_source_evidence_rejects() -> None:
    ir = _ir((_node("n1", N1, "PLAN"), _node("n2", N2, "BUILD", BUILD.span_id)))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "node n1: empty_label_without_source_occurrence"


def test_label_must_be_verbatim_span_text() -> None:
    ir = _ir((_node("n1", N1, "Plan", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "node n1: label_is_not_verbatim_source_text"


def test_node_bbox_needs_a_native_shape_within_tolerance() -> None:
    shifted: Bounds = (23.0, 73.0, 93.0, 103.0)
    svg = crop_svg(rect_path(N1)).encode()
    ir = _ir((_node("n1", shifted, "PLAN", PLAN.span_id),))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=(PLAN,), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "node n1: no_native_shape_matches_bbox"

    doubled = crop_svg(rect_path(N1), rect_path((21.0, 71.0, 91.0, 101.0))).encode()
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=doubled,
            spans=(PLAN,),
            ir=_ir((_node("n1", N1, "PLAN", PLAN.span_id),)),
            source_manifest_id=MANIFEST,
        )
    assert str(failure.value) == "node n1: ambiguous_native_shape"


def test_node_bbox_must_lie_inside_the_object() -> None:
    outside: Bounds = (20.0, 20.0, 90.0, 50.0)
    svg = crop_svg(rect_path(outside)).encode()
    ir = _ir((_node("n1", outside, "PLAN", PLAN.span_id),))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=(PLAN,), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "node n1: bbox_outside_object"


def test_cited_span_must_exist_and_lie_inside_node_bbox() -> None:
    ir = _ir((_node("n1", N1, "PLAN", "span-gone"),))
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=_two_nodes(), spans=(PLAN,), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "node n1: cited_span_missing:span-gone"

    elsewhere = _ir((_node("n1", N1, "BUILD", BUILD.span_id),))
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(), spans=(PLAN, BUILD), ir=elsewhere, source_manifest_id=MANIFEST
        )
    assert str(failure.value) == "node n1: cited_span_outside_node_bbox:span-build"


def test_span_cited_twice_rejects() -> None:
    ir = _ir((_node("n1", N1, "PLAN", PLAN.span_id, PLAN.span_id),))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=crop_svg(rect_path(N1)).encode(),
            spans=(PLAN,),
            ir=ir,
            source_manifest_id=MANIFEST,
        )

    assert str(failure.value) == "node n1: span_cited_twice:span-plan"


def test_uncited_span_inside_object_rejects() -> None:
    caption = TextSpan("span-note", "NOTE", (100.0, 81.0, 120.0, 91.0))
    ir = _ir((_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD, caption),
            ir=ir,
            source_manifest_id=MANIFEST,
        )

    assert str(failure.value) == "object: uncited_source_span:span-note"


def test_overlapping_nodes_reject() -> None:
    overlapping: Bounds = (80.0, 70.0, 150.0, 100.0)
    svg = crop_svg(rect_path(N1), rect_path(overlapping)).encode()
    other = TextSpan("span-build", "BUILD", (100.0, 81.0, 130.0, 91.0))
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", overlapping, "BUILD", "span-build"))
    )

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=(PLAN, other), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "node n1: overlaps_node:n2"


def test_node_id_must_be_path_safe() -> None:
    ir = _ir((_node("node a.b", N1, "PLAN", PLAN.span_id),))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=crop_svg(rect_path(N1)).encode(),
            spans=(PLAN,),
            ir=ir,
            source_manifest_id=MANIFEST,
        )

    assert str(failure.value) == "node node a.b: node_id_is_not_path_safe"


def test_edge_needs_connector_touching_both_nodes() -> None:
    detached = crop_svg(
        rect_path(N1),
        rect_path(N2),
        line_path((95.0, 85.0), (142.0, 85.0)),
        triangle_path(*ARROW),
    ).encode()
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)),
        (_edge("n1", "n2"),),
    )

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=detached, spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "edge 0: no_connector_between_nodes"


def test_edge_needs_arrowhead_pointing_to_target() -> None:
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)),
        (_edge("n1", "n2"),),
    )
    headless = crop_svg(rect_path(N1), rect_path(N2), line_path(*LINE)).encode()

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=headless, spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "edge 0: no_arrowhead_pointing_to_target"

    reversed_head = crop_svg(
        rect_path(N1),
        rect_path(N2),
        line_path(*LINE),
        triangle_path((150.0, 81.0), (150.0, 89.0), (142.0, 85.0)),
    ).encode()
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=reversed_head, spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "edge 0: no_arrowhead_pointing_to_target"


def test_bidirectional_edges_share_line_but_not_arrowheads() -> None:
    svg = crop_svg(
        rect_path(N1),
        rect_path(N2),
        line_path((92.0, 85.0), (148.0, 85.0)),
        triangle_path((92.0, 81.0), (92.0, 89.0), (84.0, 85.0)),
        triangle_path((148.0, 81.0), (148.0, 89.0), (156.0, 85.0)),
    ).encode()
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)),
        (_edge("n1", "n2"), _edge("n2", "n1")),
    )

    qualified = qualify_diagram(svg=svg, spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST)

    first, second = qualified.qualification.edges
    assert first.line.path_index == second.line.path_index
    assert first.arrowhead.path_index != second.arrowhead.path_index
    assert (first.tip, second.tip) == ((156.0, 85.0), (84.0, 85.0))
    assert qualified.description.text == (
        "Diagram with 2 nodes and 2 edges: PLAN; BUILD. PLAN -> BUILD. BUILD -> PLAN."
    )


def test_connector_crossing_third_node_rejects() -> None:
    middle: Bounds = (100.0, 70.0, 140.0, 100.0)
    mid_span = TextSpan("span-mid", "MID", (105.0, 81.0, 125.0, 91.0))
    svg = crop_svg(
        rect_path(N1),
        rect_path(middle),
        rect_path(N2),
        line_path(*LINE),
        triangle_path(*ARROW),
    ).encode()
    ir = _ir(
        (
            _node("n1", N1, "PLAN", PLAN.span_id),
            _node("n3", middle, "MID", mid_span.span_id),
            _node("n2", N2, "BUILD", BUILD.span_id),
        ),
        (_edge("n1", "n2"),),
    )

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=svg, spans=(PLAN, mid_span, BUILD), ir=ir, source_manifest_id=MANIFEST)

    assert str(failure.value) == "edge 0: connector_crosses_node:n3"


def test_self_loop_and_duplicate_edge_reject() -> None:
    nodes = (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD),
            ir=_ir(nodes, (_edge("n1", "n1"),)),
            source_manifest_id=MANIFEST,
        )
    assert str(failure.value) == "edge 0: self_loop"

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD),
            ir=_ir(nodes, (_edge("n1", "n2"), _edge("n1", "n2"))),
            source_manifest_id=MANIFEST,
        )
    assert str(failure.value) == "edge 1: duplicate_edge"


def test_edge_endpoints_must_name_known_nodes() -> None:
    nodes = (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD),
            ir=_ir(nodes, (_edge("n1", "n9"),)),
            source_manifest_id=MANIFEST,
        )

    assert str(failure.value) == "edge 0: unknown_node:n9"


def test_edge_label_must_pair_with_verbatim_source_spans() -> None:
    nodes = (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD),
            ir=_ir(nodes, (_edge("n1", "n2", label="then"),)),
            source_manifest_id=MANIFEST,
        )
    assert str(failure.value) == "edge 0: label_is_not_verbatim_source_text"

    then = TextSpan("span-then", "THEN", (100.0, 81.0, 130.0, 91.0))
    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(
            svg=_two_nodes(),
            spans=(PLAN, BUILD, then),
            ir=_ir(nodes, (_edge("n1", "n2", label="then", span_ids=("span-then",)),)),
            source_manifest_id=MANIFEST,
        )
    assert str(failure.value) == "edge 0: label_is_not_verbatim_source_text"

    qualified = qualify_diagram(
        svg=_two_nodes(),
        spans=(PLAN, BUILD, then),
        ir=_ir(nodes, (_edge("n1", "n2", label="THEN", span_ids=("span-then",)),)),
        source_manifest_id=MANIFEST,
    )
    assert qualified.qualification.source_span_ids == ("span-plan", "span-build", "span-then")


def test_unparseable_and_shapeless_svg_reject() -> None:
    ir = _ir((_node("n1", N1, "PLAN", PLAN.span_id),))

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=b"<svg", spans=(PLAN,), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "object: unparseable_svg"

    with pytest.raises(DiagramQualificationError) as failure:
        qualify_diagram(svg=crop_svg().encode(), spans=(PLAN,), ir=ir, source_manifest_id=MANIFEST)
    assert str(failure.value) == "object: no_native_shapes"


def test_qualification_is_deterministic_and_replayable() -> None:
    ir = _ir(
        (_node("n1", N1, "PLAN", PLAN.span_id), _node("n2", N2, "BUILD", BUILD.span_id)),
        (_edge("n1", "n2"),),
    )
    first = qualify_diagram(
        svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST
    )
    second = qualify_diagram(
        svg=_two_nodes(), spans=(PLAN, BUILD), ir=ir, source_manifest_id=MANIFEST
    )

    assert first == second
    adapter = TypeAdapter(DiagramQualification)
    assert adapter.validate_json(adapter.dump_json(first.qualification)) == first.qualification
