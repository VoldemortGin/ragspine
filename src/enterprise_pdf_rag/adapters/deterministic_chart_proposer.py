"""Propose a simple chart's IR from its own print, before any model is asked (ADR 0037).

Two grammars, both read straight off the saved crop: an upright bar chart whose every bar
prints its category below and its percent label above (``bar_geometry``), and a two-sector
donut whose every sector carries its percent label inside and its category beside it
(``donut_geometry``). Geometry only decides *which printed label belongs to which mark*; every
number is a printed source span read verbatim (ADR 0009) — never a bar height, an arc, a colour
or an axis interpolation.

A proposal is only kept when it passes the very qualification the model's IR would face
(``admit_proposal``); anything the rules cannot settle raises ``ProposalRejected`` with a
machine-readable code, and the caller falls back to ``ModelChartExtractor`` unchanged.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from math import hypot
from typing import Literal
from xml.etree import ElementTree

import pdfspine

from enterprise_pdf_rag.adapters.bar_geometry import (
    NativeBarPaint,
    _rectangle,
    match_direct_bar_labels,
)
from enterprise_pdf_rag.adapters.chart_semantics import describe_from_ir
from enterprise_pdf_rag.adapters.donut_geometry import (
    NativeSector,
    _inside,
    _intersects,
    contains,
    native_donut_geometry,
)
from enterprise_pdf_rag.adapters.donut_qualification import (
    DonutQualification,
    _aligned_external,
    _corridor,
    _full_spans,
)
from enterprise_pdf_rag.adapters.figure_label_qualification import qualify_source_labels
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from enterprise_pdf_rag.adapters.source_paint import (
    SourcePaintProof,
    _native_paths,
    build_source_paint_proof,
)
from enterprise_pdf_rag.adapters.source_paint_bar import _transformed
from ragspine.extraction.evidence.document.models import Bounds
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    Evidence,
    ExecutionMode,
    NumericObservation,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from ragspine.extraction.evidence.figures.source_label_match import match_source_value

PROPOSER_PRODUCER = "deterministic-chart-proposer/v1"
BAR_RULE = "direct-percent-bar-labels-v1"
DONUT_RULE = "two-sector-donut-direct-labels-v1"
_PERCENT = re.compile(r"[0-9]+(?:\.[0-9]+)?%")
_PERIOD = re.compile(r"[12]H[0-9]{2}")
_CODE = re.compile(r"[a-z0-9_]+")
_CONFIDENCE = Confidence(
    None,
    "deterministic-chart-proposer/v1: printed source occurrence assigned by native geometry; "
    "not a probability, still pending qualification",
)

type QualificationPolicy = Literal["none", "source-labels-only", "donut"]


class ProposalRejected(ValueError):
    """The rules could not settle this chart; ``code`` is machine-readable, never source text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChartProposal:
    chart: ChartIR
    description: TextDescription
    rule: str


def _code(error: BaseException) -> str:
    """A geometry failure's own code, or its type name when the message is not a code."""
    message = str(error)
    return message if _CODE.fullmatch(message) else type(error).__name__


def _evidence(*elements: SvgElement) -> Evidence:
    return Evidence(
        tuple(element.element_id for element in elements), Verification.PENDING, _CONFIDENCE
    )


def _field(element: SvgElement) -> TextField:
    return TextField(element.text, _evidence(element))


def _fragments(prepared: PreparedFigure, literal: SvgElement) -> tuple[SvgElement, SvgElement]:
    """The number and ``%`` substrings ``prepare_figure`` cut from one percent label."""
    fragments = tuple(
        element
        for element in prepared.svg.elements
        if element.source_span_id == literal.source_span_id
    )
    number = next((e for e in fragments if e.text == literal.text[:-1]), None)
    unit = next((e for e in fragments if e.text == "%"), None)
    if number is None or unit is None:
        raise ProposalRejected("percent_literal_substrings_missing")
    return number, unit


def _point(
    prepared: PreparedFigure,
    index: int,
    series: SvgElement,
    category: SvgElement,
    literal: SvgElement,
) -> ChartPoint:
    number, unit = _fragments(prepared, literal)
    return ChartPoint(
        f"p{index + 1}",
        _field(series),
        _field(category),
        _field(unit),
        NumericObservation(Decimal(number.text), ValueKind.EXPLICIT, _evidence(number)),
    )


def _chart(
    prepared: PreparedFigure,
    grammar: str,
    rule: str,
    points: tuple[ChartPoint, ...],
    title: SvgElement | None,
    period: SvgElement | None,
) -> ChartIR:
    return ChartIR(
        prepared.svg.binding,
        grammar,
        (),
        points,
        f"{PROPOSER_PRODUCER}:{rule}",
        Verification.PENDING,
        # The enum's offline value is the demo mode, which every qualification refuses; this
        # is a production ingest, and the producer above is what tells the two branches apart.
        ExecutionMode.PRODUCTION,
        None if title is None else _field(title),
        None if period is None else _field(period),
    )


def _grown(box: Bounds) -> Bounds:
    return (box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)


def _bar_candidates(prepared: PreparedFigure) -> tuple[NativeBarPaint, ...]:
    """Every coloured fill in the crop must be an upright rectangle; nothing is ignored by colour.

    Only three kinds of paint are passed over: unfilled strokes (axes, gridlines), a fill lying
    inside one printed span's box (its glyph outlines) and a fill under the whole crop (a
    background). A bar touching any printed span — a label inside a bar, a stacked segment —
    is refused rather than guessed around.
    """
    region = prepared.svg.source.bbox
    text = tuple(span.bbox for span in prepared.paint_text_spans)
    bars: list[NativeBarPaint] = []
    for path in _native_paths(prepared.crop_svg):
        if path.fill == "none" or not _intersects(path.bounds, region):
            continue
        if any(_inside(path.bounds, _grown(box)) for box in text) or _inside(region, path.bounds):
            continue
        if path.opacity != 1:
            raise ProposalRejected("translucent_fill")
        commands = _transformed(path.commands, path.matrix)
        try:
            bounds = _rectangle(commands)
        except ValueError:
            raise ProposalRejected("non_rectangular_fill") from None
        if not _inside(bounds, region) or any(not _inside(bounds, clip) for clip in path.clips):
            raise ProposalRejected("bar_fill_clipped")
        if any(_intersects(bounds, box) for box in text):
            raise ProposalRejected("bar_fill_overlaps_text")
        bars.append(NativeBarPaint(path.reference, commands))
    if len(bars) < 2:
        raise ProposalRejected("at_least_two_bars_required")
    return tuple(bars)


def _bar(prepared: PreparedFigure) -> ChartIR:
    full = _full_spans(prepared.svg)
    by_span = {element.source_span_id: element for element in full}
    geometry = match_direct_bar_labels(
        bars=_bar_candidates(prepared),
        spans=tuple(span for span in prepared.paint_text_spans if span.span_id in by_span),
        region=prepared.svg.source.bbox,
    )
    if any(point.literal is None for point in geometry.points):
        raise ProposalRejected("bar_value_label_missing")
    used = {point.category.span_id for point in geometry.points} | {
        point.literal.span_id for point in geometry.points if point.literal is not None
    }
    top = min(
        min(point.bar.bbox[1], point.literal.bbox[1])
        for point in geometry.points
        if point.literal is not None
    )
    rest = tuple(element for element in full if element.source_span_id not in used)
    if len(rest) != 1 or rest[0].anchor.bbox[3] > top:
        raise ProposalRejected("bar_title_scope_ambiguous")
    title = rest[0]
    points = tuple(
        _point(
            prepared,
            index,
            title,
            by_span[point.category.span_id],
            by_span[point.literal.span_id],
        )
        for index, point in enumerate(geometry.points)
        if point.literal is not None
    )
    return _chart(prepared, "bar", BAR_RULE, points, title, None)


def _donut_geometry(
    prepared: PreparedFigure, proof: SourcePaintProof | None
) -> tuple[Bounds, tuple[float, float], float, tuple[NativeSector, ...]]:
    geometry = native_donut_geometry(
        prepared.crop_svg,
        prepared.svg.source.bbox,
        prepared.view.native_svg_digest,
        text_spans=prepared.paint_text_spans,
        excluded_span_ids=prepared.excluded_span_ids,
        proven_glyph_refs=tuple(glyph.native_path_ref for glyph in proof.glyphs) if proof else (),
        transparent_refs=tuple(
            vector.native_path_ref for vector in proof.vectors if vector.role == "transparent"
        )
        if proof
        else (),
        source_proof_id=proof.proof_id if proof else None,
    )
    sectors = geometry.sectors
    outer = (
        min(s.bbox[0] for s in sectors),
        min(s.bbox[1] for s in sectors),
        max(s.bbox[2] for s in sectors),
        max(s.bbox[3] for s in sectors),
    )
    center = ((outer[0] + outer[2]) / 2, (outer[1] + outer[3]) / 2)
    inner = min(
        hypot(p[0] - center[0], p[1] - center[1]) for sector in sectors for p in sector.points
    )
    return outer, center, inner, tuple(sectors)


def _donut(prepared: PreparedFigure, source_pdf: Callable[[], bytes] | None) -> ChartIR:
    try:
        outer, center, inner, sectors = _donut_geometry(prepared, None)
    except ValueError as error:
        # Neutral paint (glyph outlines) is only ever explained by the source replay proof.
        if source_pdf is None or str(error) != "unexplained_source_paint_requires_review":
            raise
        # The geometry needs exactly two coloured sectors; replay the page only when it could.
        coloured = sum(
            path.fill.lower() not in {"none", "#000000", "#ffffff"}
            and _intersects(path.bounds, prepared.svg.source.bbox)
            for path in _native_paths(prepared.crop_svg)
        )
        if coloured != 2:
            raise ProposalRejected("two_coloured_sectors_required") from None
        try:
            proof = build_source_paint_proof(source_pdf(), prepared=prepared)
        except (
            pdfspine.PdfError,
            OSError,
            ValueError,
            LookupError,
            TypeError,
            RuntimeError,
        ) as failure:
            raise ProposalRejected("source_paint_proof_unavailable") from failure
        if proof.coverage != "complete_source_paint":
            raise ProposalRejected("source_paint_proof_incomplete") from None
        outer, center, inner, sectors = _donut_geometry(prepared, proof)
    full = _full_spans(prepared.svg)
    central = tuple(
        e
        for e in full
        if all(
            hypot(x - center[0], y - center[1]) < inner - 1
            for x, y in (
                (e.anchor.bbox[0], e.anchor.bbox[1]),
                (e.anchor.bbox[2], e.anchor.bbox[3]),
            )
        )
    )
    literals = tuple(e for e in full if _PERCENT.fullmatch(e.text) and e not in central)
    if len(literals) != len(sectors):
        raise ProposalRejected("one_percent_label_per_sector_required")
    categories = tuple(
        e
        for e in full
        if e not in (*literals, *central)
        and any(_aligned_external(e, value, outer) for value in literals)
    )
    pairs: list[tuple[SvgElement, SvgElement]] = []
    used_sectors: set[int] = set()
    for literal in literals:
        box = literal.anchor.bbox
        inside = tuple(
            index
            for index, sector in enumerate(sectors)
            if all(
                contains(sector.points, p, margin=1.0)
                for p in (
                    ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
                    (box[0], box[1]),
                    (box[2], box[1]),
                    (box[0], box[3]),
                    (box[2], box[3]),
                )
            )
        )
        beside = tuple(e for e in categories if _corridor(e, literal, full, outer))
        if len(inside) != 1 or len(beside) != 1:
            raise ProposalRejected("donut_label_relation_ambiguous")
        used_sectors.add(inside[0])
        pairs.append((beside[0], literal))
    if len(used_sectors) != len(sectors) or len({c.source_span_id for c, _ in pairs}) != len(pairs):
        raise ProposalRejected("donut_sector_label_mapping_not_one_to_one")
    periods = tuple(e for e in central if _PERIOD.fullmatch(e.text))
    metrics = tuple(e for e in central if e not in periods)
    used = {e.source_span_id for pair in pairs for e in pair} | {e.source_span_id for e in central}
    rest = tuple(e for e in full if e.source_span_id not in used)
    if (
        len(periods) > 1
        or len(metrics) > 1
        or len(rest) > 1
        or any(e.anchor.bbox[3] > outer[1] for e in rest)
    ):
        raise ProposalRejected("donut_title_metric_or_period_ambiguous")
    title = rest[0] if rest else None
    series = metrics[0] if metrics else title
    if series is None:
        raise ProposalRejected("donut_series_label_missing")
    points = tuple(
        _point(prepared, index, series, category, literal)
        for index, (category, literal) in enumerate(pairs)
    )
    return _chart(prepared, "donut", DONUT_RULE, points, title, periods[0] if periods else None)


def _explicit_source_value(prepared: PreparedFigure, point: ChartPoint) -> None:
    """ADR 0009: a value is an explicit number its own cited source text prints, or nothing."""
    if point.value.kind is not ValueKind.EXPLICIT or point.value.value is None:
        raise ProposalRejected("value_not_explicit")
    if (
        match_source_value(
            prepared.svg.elements,
            point.value.value,
            point.unit.text,
            point.value.evidence.element_ids,
        )
        is None
    ):
        raise ProposalRejected("value_not_printed_by_its_source_text")


def admit_proposal(
    prepared: PreparedFigure,
    chart: ChartIR,
    description: TextDescription,
    *,
    policy: QualificationPolicy,
) -> None:
    """Keep a proposal only if the model IR's own qualification keeps every one of its points.

    ``policy`` is the adapter's: ``donut`` runs ``DonutQualification``; ``source-labels-only``
    and ``none`` (whose objects are re-proved later by ``visual_requalification``) run
    ``qualify_source_labels``. Nothing here is looser than what the model's IR faces.
    """
    if not chart.points:
        raise ProposalRejected("no_points")
    for point in chart.points:
        _explicit_source_value(prepared, point)
    try:
        projected = (
            DonutQualification(prepared).qualify_pair(prepared.svg, chart, description).chart
            if policy == "donut"
            else qualify_source_labels(prepared.svg, chart, description).chart
        )
    except ValueError as error:
        raise ProposalRejected("qualification_failed:" + _code(error)) from None
    if {point.point_id: point.value.value for point in projected.points} != {
        point.point_id: point.value.value for point in chart.points
    }:
        raise ProposalRejected("qualification_dropped_points")


def propose_chart(
    prepared: PreparedFigure,
    *,
    policy: QualificationPolicy,
    source_pdf: Callable[[], bytes] | None = None,
) -> ChartProposal:
    """A qualified-admissible bar / donut IR plus its label description, or ``ProposalRejected``.

    ``source_pdf`` is read only when a donut's glyph outlines need the source replay proof.
    """
    if prepared.page_context:
        raise ProposalRejected("page_context_present")
    if prepared.excluded_span_ids:
        raise ProposalRejected("partial_text_at_crop_edge")
    codes: list[str] = []
    builders: tuple[tuple[str, Callable[[], ChartIR]], ...] = (
        ("bar", lambda: _bar(prepared)),
        ("donut", lambda: _donut(prepared, source_pdf)),
    )
    for name, build in builders:
        # Malformed or unexpected paint is a refusal like any other, never an ingest failure.
        try:
            chart = build()
        except (ValueError, LookupError, ArithmeticError, ElementTree.ParseError) as error:
            codes.append(
                f"{name}:{error.code if isinstance(error, ProposalRejected) else _code(error)}"
            )
            continue
        description = describe_from_ir(chart)
        if description is None:
            raise ProposalRejected(f"{name}:no_label_field")
        admit_proposal(prepared, chart, description, policy=policy)
        return ChartProposal(chart, description, chart.producer.rsplit(":", 1)[1])
    raise ProposalRejected(";".join(codes))
