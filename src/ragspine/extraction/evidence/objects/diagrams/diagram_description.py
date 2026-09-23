"""Deterministic natural-language projection of a proven DiagramIR; no model runs here."""

from typing import Literal

from ragspine.extraction.evidence.figures.models import Confidence, Verification
from ragspine.extraction.evidence.objects.diagrams.diagram_models import READING_ROW_QUANTUM
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, DiagramNode, ObjectDescription

DESCRIPTION_PRODUCER = "deterministic-diagram-description-v1"
DESCRIPTION_CONFIDENCE = Confidence(
    None, "deterministic projection of proven diagram structure; no semantic inference"
)
EDGE_ARROW = " -> "


def reading_order(nodes: tuple[DiagramNode, ...]) -> tuple[DiagramNode, ...]:
    """Rows quantised to ``READING_ROW_QUANTUM`` points, then left to right, then by id."""
    return tuple(
        sorted(
            nodes,
            key=lambda node: (
                round(node.bbox[1] / READING_ROW_QUANTUM),
                node.bbox[0],
                node.node_id,
            ),
        )
    )


def diagram_description_text(ir: DiagramIR, *, language: Literal["en", "zh"] = "en") -> str:
    """Template words are the only non-verbatim tokens; every label is copied unchanged."""
    nodes = reading_order(ir.nodes)
    labels = [node.label for node in nodes]
    by_id = {node.node_id: node.label for node in ir.nodes}
    edges = [
        f"{by_id[edge.source_node_id]}{EDGE_ARROW}{by_id[edge.target_node_id]}" for edge in ir.edges
    ]
    if language == "zh":
        head = f"流程图，{len(labels)} 个节点：" + "；".join(labels) + "。"  # noqa: RUF001 — fullwidth CJK punctuation is the Chinese template, never its ASCII lookalike
        return head + ("".join(f"{edge}。" for edge in edges) if edges else "无连线。")
    count = f"{len(labels)} node{'s' if len(labels) != 1 else ''}"
    if not edges:
        return f"Diagram with {count}: " + "; ".join(labels) + ". No connecting edges."
    return (
        f"Diagram with {count} and {len(edges)} edge{'s' if len(edges) != 1 else ''}: "
        + "; ".join(labels)
        + ". "
        + " ".join(f"{edge}." for edge in edges)
    )


def describe_diagram(ir: DiagramIR, *, language: Literal["en", "zh"] = "en") -> ObjectDescription:
    """Project the proven structure; the text carries no claim the IR does not already hold."""
    return ObjectDescription(
        ir.object_id,
        ir.source,
        tuple(dict.fromkeys(span_id for node in ir.nodes for span_id in node.source_span_ids)),
        diagram_description_text(ir, language=language),
        DESCRIPTION_PRODUCER,
        DESCRIPTION_CONFIDENCE,
        Verification.VERIFIED,
    )
