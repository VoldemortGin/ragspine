"""Deterministic index text per member: the one string both retrieval channels score.

A chart's embedded description is usually its title alone (``Distribution Mix``), so
a question that names the chart's categories or values but not its title has nothing
to match. ``chart_index_text`` projects the qualified IR instead: title, period,
grammar and every point's category / series / explicit value. A chart without a
single explicit value keeps its description text, so a pending or label-only figure
never climbs the ranking on words it cannot cite. A proven diagram is projected the same
way: its node labels in reading order plus one ``<from> -> <to>`` pair per drawn edge.
Nothing here reads a store.
"""

from dataclasses import dataclass
from decimal import Decimal

from ragspine.extraction.evidence.figures.models import ChartIR, ChartPoint, ValueKind
from ragspine.extraction.evidence.objects.diagrams.diagram_description import (
    EDGE_ARROW,
    reading_order,
)
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, FormulaIR, TypedIR


@dataclass(frozen=True, slots=True)
class PageIndexContext:
    """The page context prepended to a member's index text (policy v4): none is optional."""

    display_title: str | None = None
    page_title: str | None = None
    section: str | None = None

    def header(self) -> str:
        parts = (
            " ".join(part.split())
            for part in (self.display_title, self.page_title, self.section)
            if part is not None
        )
        return " | ".join(part for part in parts if part)


def contextual_index_text(body: str, context: PageIndexContext | None) -> str:
    """``<display_title> | <page_title> | <section>`` on its own line above ``body``.

    Only the text both retrieval channels score changes; descriptions and quoted
    evidence are untouched. Without any context the body is returned as is.
    """
    header = "" if context is None else context.header()
    return f"{header}\n{body}" if header else body


def _explicit(point: ChartPoint) -> bool:
    return point.value.kind is ValueKind.EXPLICIT and point.value.value is not None


def has_citable_value(chart: ChartIR) -> bool:
    """True when at least one point carries an explicit numeric value an answer may cite."""
    return any(_explicit(point) for point in chart.points)


def _value_text(value: Decimal, unit: str) -> str:
    unit = unit.strip()
    if unit and not any(char.isalnum() for char in unit):
        return f"{value}{unit}"  # ``72%``
    return f"{value} {unit}".strip()  # ``15 US cents``


def chart_index_text(chart: ChartIR, *, fallback: str) -> str:
    """Project a chart with explicit values into searchable text; otherwise ``fallback``.

    The projection is deterministic: ``<title> <period> <grammar> chart figure`` followed
    by ``<category> <series> <value><unit>`` per point (labels only for points whose
    value is unavailable). ``fallback`` is the chart's description text, which is what
    charts embedded before this projection existed.
    """
    if not has_citable_value(chart):
        return fallback
    parts: list[str] = []
    if chart.title is not None:
        parts.append(chart.title.text)
    if chart.period is not None:
        parts.append(chart.period.text)
    parts.append(f"{chart.grammar} chart figure")
    for point in chart.points:
        parts.extend((point.category.text, point.series.text))
        if _explicit(point):
            assert point.value.value is not None
            parts.append(_value_text(point.value.value, point.unit.text))
    return " ".join(part.strip() for part in parts if part.strip())


def has_citable_structure(diagram: DiagramIR) -> bool:
    """True when at least one node label is verbatim source text an answer may cite."""
    return any(node.label.strip() and node.source_span_ids for node in diagram.nodes)


def diagram_index_text(diagram: DiagramIR, *, fallback: str) -> str:
    """Project a proven diagram into searchable text; otherwise ``fallback``.

    The projection is deterministic: ``diagram figure`` followed by every node label in
    reading order and then ``<from> -> <to>`` per proven edge, the same pair a
    ``diagram_edge`` claim cites. ``fallback`` is the member's description text.
    """
    if not has_citable_structure(diagram):
        return fallback
    by_id = {node.node_id: node.label for node in diagram.nodes}
    parts = ["diagram figure", *(node.label for node in reading_order(diagram.nodes))]
    parts.extend(
        f"{by_id[edge.source_node_id]}{EDGE_ARROW}{by_id[edge.target_node_id]}"
        for edge in diagram.edges
    )
    return " ".join(part.strip() for part in parts if part.strip())


def formula_index_text(formula: FormulaIR, *, fallback: str) -> str:
    """Project a proven formula into searchable text; otherwise ``fallback``.

    The projection is deterministic: the readable transcription, its linear form, the
    literal word ``formula`` and every distinct token text, so a question that names a
    symbol or an operand rather than the surrounding prose has something to match. A
    model-only ``FormulaIR`` carries no token, so it keeps its description text.
    """
    if not formula.tokens or formula.linear is None or formula.readable is None:
        return fallback
    parts = [formula.readable, formula.linear, "formula"]
    parts.extend(dict.fromkeys(token.text for token in formula.tokens))
    return " ".join(part.strip() for part in parts if part.strip())


def member_index_text(ir: TypedIR, description_text: str) -> str:
    """Text / list / group / table members index their description; figures are projected."""
    if isinstance(ir, ChartIR):
        return chart_index_text(ir, fallback=description_text)
    if isinstance(ir, DiagramIR):
        return diagram_index_text(ir, fallback=description_text)
    if isinstance(ir, FormulaIR):
        return formula_index_text(ir, fallback=description_text)
    return description_text
