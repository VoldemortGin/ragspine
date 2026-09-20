"""Prove a DiagramIR against its own SVG crop and source spans; fail closed as a whole."""

import re
from dataclasses import dataclass, replace
from math import hypot
from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.diagram_geometry import (
    arrowhead,
    native_shapes,
    rectangle_like,
    segment_crosses,
    straight_lines,
    tip_and_base,
    touches,
)
from enterprise_pdf_rag.adapters.donut_geometry import _intersects
from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.diagram_description import describe_diagram
from enterprise_pdf_rag.processing.diagram_models import (
    ARROW_JOIN_TOLERANCE,
    CONNECT_TOLERANCE,
    DIAGRAM_METHOD,
    NODE_BBOX_TOLERANCE,
    NODE_ID_PATTERN,
    SPAN_INSIDE_TOLERANCE,
    DiagramQualification,
    EdgeEvidence,
    NodeEvidence,
    PathEvidence,
    Point,
)
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, DiagramNode, ObjectDescription


class DiagramQualificationError(ValueError):
    """``str(error)`` is the verbatim stage diagnostic: ``<subject>: <reason>``."""


@dataclass(frozen=True, slots=True)
class QualifiedDiagram:
    """The three products of one proof: the verified IR, its projection and the receipt."""

    ir: DiagramIR
    description: ObjectDescription
    qualification: DiagramQualification


def _fail(subject: str, reason: str) -> DiagramQualificationError:
    return DiagramQualificationError(f"{subject}: {reason}")


def _normal(value: str) -> str:
    return " ".join(value.split())


def _label_matches_spans(label: str, cited: tuple[TextSpan, ...]) -> bool:
    """Same rule as ``visual_semantics._label_matches``: fold whitespace, keep case."""
    observed = tuple(_normal(span.text) for span in cited)
    return bool(cited) and _normal(label) in (*observed, " ".join(observed))


def _clip(bounds: Bounds, object_bbox: Bounds) -> Bounds:
    return (
        max(bounds[0], object_bbox[0]),
        max(bounds[1], object_bbox[1]),
        min(bounds[2], object_bbox[2]),
        min(bounds[3], object_bbox[3]),
    )


def _same_box(clipped: Bounds, bbox: Bounds) -> bool:
    return all(
        abs(actual - expected) <= NODE_BBOX_TOLERANCE
        for actual, expected in zip(clipped, bbox, strict=True)
    )


def _connector(
    lines: tuple[PathEvidence, ...], source_bbox: Bounds, target_bbox: Bounds
) -> tuple[PathEvidence, Point, Point] | None:
    """The lowest-indexed polyline leaving the source node; its far end feeds the arrowhead."""
    for line in lines:
        for start, end in ((line.points[0], line.points[-1]), (line.points[-1], line.points[0])):
            if touches(source_bbox, start, tolerance=CONNECT_TOLERANCE) and not touches(
                target_bbox, start, tolerance=CONNECT_TOLERANCE
            ):
                return line, start, end
    return None


def _arrowhead_into(
    heads: tuple[PathEvidence, ...],
    used: set[int],
    source_bbox: Bounds,
    target_bbox: Bounds,
    end: Point,
) -> tuple[PathEvidence, Point, Point] | None:
    """An unused triangle joined to ``end`` whose tip lands in the target and not the source."""
    for head in heads:
        if head.path_index in used:
            continue
        try:
            tip, base_mid = tip_and_base(head)
        except ValueError:
            continue
        if (
            touches(target_bbox, tip, tolerance=CONNECT_TOLERANCE)
            and not touches(source_bbox, tip, tolerance=CONNECT_TOLERANCE)
            and hypot(base_mid[0] - end[0], base_mid[1] - end[1]) <= ARROW_JOIN_TOLERANCE
        ):
            return head, tip, base_mid
    return None


def _prove_node(
    node: DiagramNode,
    nodes: tuple[DiagramNode, ...],
    object_bbox: Bounds,
    page_spans: dict[str, TextSpan],
    inside_ids: frozenset[str],
    rectangles: tuple[PathEvidence, ...],
    cited: dict[str, None],
    used_shapes: set[int],
) -> NodeEvidence:
    subject = f"node {node.node_id}"
    if re.fullmatch(NODE_ID_PATTERN, node.node_id) is None:
        raise _fail(subject, "node_id_is_not_path_safe")
    if not contains(object_bbox, node.bbox):
        raise _fail(subject, "bbox_outside_object")
    if not node.label.strip() or not node.source_span_ids:
        raise _fail(subject, "empty_label_without_source_occurrence")
    cited_spans: list[TextSpan] = []
    for span_id in node.source_span_ids:
        span = page_spans.get(span_id)
        if span is None:
            raise _fail(subject, f"cited_span_missing:{span_id}")
        if span_id not in inside_ids or not contains(
            node.bbox, span.bbox, tolerance=SPAN_INSIDE_TOLERANCE
        ):
            raise _fail(subject, f"cited_span_outside_node_bbox:{span_id}")
        cited_spans.append(span)
    if not _label_matches_spans(node.label, tuple(cited_spans)):
        raise _fail(subject, "label_is_not_verbatim_source_text")
    for span_id in node.source_span_ids:
        if span_id in cited:
            raise _fail(subject, f"span_cited_twice:{span_id}")
        cited[span_id] = None
    matches = tuple(
        rectangle
        for rectangle in rectangles
        if rectangle.path_index not in used_shapes
        and _same_box(_clip(rectangle.bounds, object_bbox), node.bbox)
    )
    if not matches:
        raise _fail(subject, "no_native_shape_matches_bbox")
    if len(matches) > 1:
        raise _fail(subject, "ambiguous_native_shape")
    used_shapes.add(matches[0].path_index)
    for other in nodes:
        if other.node_id != node.node_id and _intersects(node.bbox, other.bbox):
            raise _fail(subject, f"overlaps_node:{other.node_id}")
    return NodeEvidence(
        node.node_id,
        tuple(node.source_span_ids),
        matches[0],
        _clip(matches[0].bounds, object_bbox),
    )


def _prove_edge_label(
    subject: str,
    label: str | None,
    span_ids: tuple[str, ...],
    page_spans: dict[str, TextSpan],
    inside_ids: frozenset[str],
    cited: dict[str, None],
) -> None:
    if (label is None) != (not span_ids):
        raise _fail(subject, "label_is_not_verbatim_source_text")
    if label is None:
        return
    cited_spans: list[TextSpan] = []
    for span_id in span_ids:
        span = page_spans.get(span_id)
        if span is None:
            raise _fail(subject, f"cited_span_missing:{span_id}")
        if span_id not in inside_ids:
            raise _fail(subject, f"cited_span_outside_object_bbox:{span_id}")
        cited_spans.append(span)
    if not _label_matches_spans(label, tuple(cited_spans)):
        raise _fail(subject, "label_is_not_verbatim_source_text")
    for span_id in span_ids:
        if span_id in cited:
            raise _fail(subject, f"span_cited_twice:{span_id}")
        cited[span_id] = None


def qualify_diagram(
    *,
    svg: bytes,
    spans: tuple[TextSpan, ...],
    ir: DiagramIR,
    source_manifest_id: str,
) -> QualifiedDiagram:
    """Raise ``DiagramQualificationError`` on the first failed rule; never partial."""
    object_bbox = ir.source.bbox
    try:
        shapes = native_shapes(svg.decode())
    except (ElementTree.ParseError, UnicodeDecodeError):
        raise _fail("object", "unparseable_svg") from None
    if not shapes:
        raise _fail("object", "no_native_shapes")
    page_spans = {span.span_id: span for span in spans}
    inside = tuple(
        span for span in spans if contains(object_bbox, span.bbox, tolerance=SPAN_INSIDE_TOLERANCE)
    )
    inside_ids = frozenset(span.span_id for span in inside)
    rectangles = tuple(
        evidence for shape in shapes if (evidence := rectangle_like(shape, object_bbox)) is not None
    )
    lines = tuple(evidence for shape in shapes if (evidence := straight_lines(shape)) is not None)
    heads = tuple(evidence for shape in shapes if (evidence := arrowhead(shape)) is not None)
    cited: dict[str, None] = {}
    used_shapes: set[int] = set()
    node_evidence = tuple(
        _prove_node(
            node,
            ir.nodes,
            object_bbox,
            page_spans,
            inside_ids,
            rectangles,
            cited,
            used_shapes,
        )
        for node in ir.nodes
    )
    by_id = {node.node_id: node for node in ir.nodes}
    used_heads: set[int] = set()
    seen_pairs: set[tuple[str, str]] = set()
    edge_evidence: list[EdgeEvidence] = []
    for index, edge in enumerate(ir.edges):
        subject = f"edge {index}"
        if edge.source_node_id == edge.target_node_id:
            raise _fail(subject, "self_loop")
        pair = (edge.source_node_id, edge.target_node_id)
        if pair in seen_pairs:
            raise _fail(subject, "duplicate_edge")
        seen_pairs.add(pair)
        for node_id in pair:
            if node_id not in by_id:
                raise _fail(subject, f"unknown_node:{node_id}")
        source_bbox, target_bbox = by_id[pair[0]].bbox, by_id[pair[1]].bbox
        connector = _connector(lines, source_bbox, target_bbox)
        if connector is None:
            raise _fail(subject, "no_connector_between_nodes")
        line, _start, end = connector
        joined = _arrowhead_into(heads, used_heads, source_bbox, target_bbox, end)
        if joined is None:
            raise _fail(subject, "no_arrowhead_pointing_to_target")
        head, tip, base_mid = joined
        used_heads.add(head.path_index)
        for other_id, other in by_id.items():
            if other_id in pair:
                continue
            for first, last in zip(line.points, line.points[1:], strict=False):
                if segment_crosses(other.bbox, first, last, shrink=CONNECT_TOLERANCE):
                    raise _fail(subject, f"connector_crosses_node:{other_id}")
        _prove_edge_label(subject, edge.label, edge.source_span_ids, page_spans, inside_ids, cited)
        edge_evidence.append(EdgeEvidence(index, pair[0], pair[1], line, head, tip, base_mid, end))
    for span in inside:
        if span.span_id not in cited:
            raise _fail("object", f"uncited_source_span:{span.span_id}")
    qualification = DiagramQualification(
        ir.object_id,
        ir.source,
        source_manifest_id,
        tuple(cited),
        node_evidence,
        tuple(edge_evidence),
    )
    proven = replace(
        ir,
        edges=tuple(replace(edge, verification=Verification.VERIFIED) for edge in ir.edges),
        verification=Verification.VERIFIED,
        diagnostics=(*ir.diagnostics, f"Structure proven independently: {DIAGRAM_METHOD}"),
    )
    return QualifiedDiagram(proven, describe_diagram(proven), qualification)
