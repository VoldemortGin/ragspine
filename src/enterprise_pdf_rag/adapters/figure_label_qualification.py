"""Qualify what a figure prints verbatim — its labels, and under the newer scope its points.

Two policies live here and both stay reachable for ever, because
``chart_publication.resolve_chart_member`` re-derives a published receipt and compares it
byte for byte:

``figure-source-labels-only-v1``
    One description claim, one whole source occurrence, nothing numeric. Frozen.
``source-labels-and-verbatim-points-v1``
    Labels may be a run of up to three adjacent source occurrences and may come from the
    ChartIR's printed fields as well as the description; a chart *point* survives into the
    projection when its category and its value are both printed verbatim in the figure.

What the newer scope proves, and what it does not
-------------------------------------------------
**Proved:** every string it projects — label, title, period, category, series, unit and
number — is printed verbatim inside this figure's own region, by source text observations
the receipt names element by element. Nothing is inferred, repaired or re-rendered.

**Not proved:** that a number belongs to the category printed beside it. That association
remains the model's assertion. ADR 0008 is explicit that a readable label plus proximity
may not establish it, and only the geometry + source-paint proofs
(``donut_qualification`` / ``bar_qualification``) settle it. Hence: a scope name distinct
from ``explicit-distribution-shares``, never that scope's receipt type, a projected chart
left ``PENDING``, and no place on the financial allow-list in
``processing/retrieval.require_financial_qualification``.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from ragspine.extraction.evidence.figures.models import (
    ChartAxis,
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    EvidenceKind,
    ExecutionMode,
    FailureCode,
    FieldOccurrence,
    FigureError,
    FigureQualification,
    NumericObservation,
    SvgArtifact,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from ragspine.extraction.evidence.figures.source_label_match import (
    fold_whitespace,
    match_source_label,
    match_source_value,
    trailing_parenthetical,
    window_text,
    windows,
    without_trailing_parenthetical,
)
from ragspine.extraction.evidence.figures.validation import validate_svg

FIGURE_LABEL_SCOPE = "figure-source-labels-only-v1"
FIGURE_POINT_SCOPE = "source-labels-and-verbatim-points-v1"
FIGURE_LABEL_SCOPES = frozenset({FIGURE_LABEL_SCOPE, FIGURE_POINT_SCOPE})
_METHOD = "exact-single-source-occurrence-label-v1; no numeric or financial relations"
_METHOD_POINTS = (
    "source-labels-and-verbatim-points-v1; labels and point categories are runs of at most 3 "
    "adjacent source occurrences; a point value is a printed number in its own window; "
    "no category-to-value association is proven"
)
_NUMERIC_TEXT = re.compile(r"\d|[%$€£¥]")
_CONFIDENCE = Confidence(
    None,
    "deterministic exact source occurrence label; no semantic inference",
)
_CONFIDENCE_POINTS = Confidence(
    None,
    "deterministic verbatim source occurrence; the category-to-value association is unproven",
)
_MASKED_PRODUCER = "source-labels-only-unknown-chart-v1:"
_POINTS_PRODUCER = "verbatim-source-points-v1:"


@dataclass(frozen=True, slots=True)
class QualifiedLabelProjection:
    chart: ChartIR
    description: TextDescription
    receipt: FigureQualification
    raw_chart_id: str
    raw_description_id: str
    excluded_claim_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Projection:
    """What one policy produced: the projected chart, its claims, its receipt rows, its refusals."""

    chart: ChartIR
    claims: tuple[DescriptionClaim, ...]
    fields: tuple[FieldOccurrence, ...]
    geometry: tuple[str, ...]
    excluded: tuple[str, ...]
    method: str


@dataclass(frozen=True, slots=True)
class _PointMatch:
    """Every window that had to print something for one point to survive."""

    category: tuple[SvgElement, ...]
    value: tuple[SvgElement, ...]
    number: Decimal
    unit: tuple[SvgElement, ...] | None
    series: tuple[SvgElement, ...] | None


def _fail(reason: str) -> FigureError:
    return FigureError(FailureCode.UNVERIFIED, reason)


def _ids(window: Sequence[SvgElement]) -> tuple[str, ...]:
    return tuple(element.element_id for element in window)


def _refs(origin: str, window: Sequence[SvgElement]) -> Iterator[str]:
    for element in window:
        yield f"{origin}:source-span:{element.source_span_id}:element:{element.element_id}"


# --------------------------------------------------------------------------------------
# figure-source-labels-only-v1 — frozen
# --------------------------------------------------------------------------------------


def _exact_label(svg: SvgArtifact, claim: DescriptionClaim) -> SvgElement:
    if claim.value is not None:
        raise _fail("numeric_claim_not_a_label")
    if _NUMERIC_TEXT.search(claim.text):
        raise _fail("numeric_text_not_a_label")
    ids = claim.evidence.element_ids
    if claim.evidence.verification is Verification.REJECTED or len(ids) != len(set(ids)):
        raise _fail("rejected_or_duplicate_source_evidence")
    elements = {element.element_id: element for element in svg.elements}
    cited = tuple(elements[element_id] for element_id in ids if element_id in elements)
    if len(cited) != len(ids):
        raise _fail("missing_source_evidence")
    matching = tuple(
        element
        for element in cited
        if element.text == claim.text
        and element.evidence_kind is EvidenceKind.SOURCE_TEXT_OBSERVATION
        and element.source_span_id is not None
    )
    if len(matching) != 1 or any(
        element.source_span_id != matching[0].source_span_id for element in cited
    ):
        raise _fail("claim_is_not_one_exact_source_occurrence")
    return matching[0]


def _project_v1(svg: SvgArtifact, chart: ChartIR, description: TextDescription) -> _Projection:
    """One claim, one whole source occurrence. ChartIR is lineage only, never a claim source."""
    claims: list[DescriptionClaim] = []
    fields: list[FieldOccurrence] = []
    geometry: list[str] = []
    excluded: list[str] = []
    used_elements: set[str] = set()
    for index, claim in enumerate(description.claims):
        try:
            element = _exact_label(svg, claim)
            if element.element_id in used_elements:
                raise _fail("duplicate_label_occurrence")
        except FigureError as error:
            excluded.append(f"claims[{index}]:{error}")
            continue
        used_elements.add(element.element_id)
        output_index = len(claims)
        evidence = Evidence((element.element_id,), Verification.VERIFIED, _CONFIDENCE)
        claims.append(
            replace(
                claim,
                evidence=evidence,
                series=None,
                category=None,
                unit=None,
                value=None,
                period=None,
            )
        )
        fields.append(FieldOccurrence(f"claims.{output_index}.text", (element.element_id,)))
        geometry.append(f"source-span:{element.source_span_id}:element:{element.element_id}")
    masked = replace(
        chart,
        axes=(),
        points=(),
        title=None,
        period=None,
        marks=(),
        producer=_MASKED_PRODUCER + chart.producer,
        verification=Verification.PENDING,
    )
    return _Projection(
        masked, tuple(claims), tuple(fields), tuple(geometry), tuple(excluded), _METHOD
    )


# --------------------------------------------------------------------------------------
# source-labels-and-verbatim-points-v1
# --------------------------------------------------------------------------------------


def _cited(svg: SvgArtifact, evidence: Evidence) -> tuple[str, ...]:
    ids = evidence.element_ids
    if evidence.verification is Verification.REJECTED or len(ids) != len(set(ids)):
        raise _fail("rejected_or_duplicate_source_evidence")
    known = {element.element_id for element in svg.elements}
    if any(element_id not in known for element_id in ids):
        raise _fail("missing_source_evidence")
    return ids


def _verbatim(svg: SvgArtifact, text: str, evidence: Evidence) -> tuple[SvgElement, ...]:
    """The window printing ``text``, allowing one retry without a trailing parenthetical."""
    ids = _cited(svg, evidence)
    window = match_source_label(svg.elements, text, ids)
    if window is None:
        shortened = without_trailing_parenthetical(text)
        if shortened is not None:
            window = match_source_label(svg.elements, shortened, ids)
    if window is None:
        raise _fail("not_an_adjacent_source_occurrence")
    return window


def _unit_window(
    svg: SvgArtifact, unit: str, cited: Sequence[str]
) -> tuple[SvgElement, ...] | None:
    """A window printing the unit outright, or printing it as its own trailing parenthetical."""
    for window in windows(svg.elements, cited):
        text = window_text(window)
        if text == unit or trailing_parenthetical(text) == unit:
            return window
    return None


def _axis_citations(chart: ChartIR) -> tuple[str, ...]:
    """Where a unit may also be printed: the chart's own title and axis captions (R6)."""
    pool: list[str] = []
    fields = (chart.title, *(field for axis in chart.axes for field in (axis.label, axis.unit)))
    for field in fields:
        if field is None:
            continue
        pool.extend(
            element_id for element_id in field.evidence.element_ids if element_id not in pool
        )
    return tuple(pool)


def _match_point(svg: SvgArtifact, point: ChartPoint, pool: Sequence[str]) -> _PointMatch:
    if point.value.kind is not ValueKind.EXPLICIT or point.value.value is None:
        raise _fail("value_is_not_explicit_in_source")
    category = _verbatim(svg, point.category.text, point.category.evidence)
    unit = fold_whitespace(point.unit.text)
    printed = match_source_value(
        svg.elements, point.value.value, unit, _cited(svg, point.value.evidence)
    )
    if printed is None:
        raise _fail("value_is_not_printed_in_the_figure")
    value_window, number, carries_unit = printed
    unit_window: tuple[SvgElement, ...] | None = None
    if unit and not carries_unit:
        unit_window = _unit_window(svg, unit, _cited(svg, point.unit.evidence)) or _unit_window(
            svg, unit, pool
        )
        if unit_window is None:
            raise _fail("unit_is_not_printed_in_the_figure")
    elif unit:
        unit_window = value_window
    try:
        series = _verbatim(svg, point.series.text, point.series.evidence)
    except FigureError:
        series = None  # A series is never required; an unprinted one is dropped, not guessed.
    return _PointMatch(category, value_window, number, unit_window, series)


def _field(window: Sequence[SvgElement]) -> TextField:
    return TextField(
        window_text(window), Evidence(_ids(window), Verification.VERIFIED, _CONFIDENCE_POINTS)
    )


def _blank(window: Sequence[SvgElement]) -> TextField:
    """An unprinted series or unit: empty, so no projection ever prints the model's guess."""
    return TextField("", Evidence(_ids(window), Verification.VERIFIED, _CONFIDENCE_POINTS))


def _label_fields(chart: ChartIR) -> Iterator[tuple[str, TextField]]:
    """Every ChartIR field that is printed prose, in a fixed order. Never a value or a unit."""
    if chart.title is not None:
        yield "title", chart.title
    if chart.period is not None:
        yield "period", chart.period
    for index, axis in enumerate(chart.axes):
        yield f"axes.{index}.label", axis.label
        yield f"axes.{index}.unit", axis.unit
    for point in chart.points:
        yield f"points.{point.point_id}.category", point.category
        yield f"points.{point.point_id}.series", point.series


class _Receipt:
    """The receipt rows and geometry references of one projected surface, in write order."""

    def __init__(self) -> None:
        self.fields: list[FieldOccurrence] = []
        self.geometry: list[str] = []

    def record(
        self,
        path: str,
        window: Sequence[SvgElement],
        *,
        origin: str | None = None,
        printed: bool = True,
    ) -> None:
        """``path`` is what gets indexed; ``origin`` is which branch proposed it.

        ``printed=False`` closes the receipt over a field the figure never printed — an empty
        series or unit, carried by its point's value window — without claiming its geometry.
        """
        self.fields.append(FieldOccurrence(path, _ids(window)))
        if printed:
            self.geometry.extend(_refs(origin if origin is not None else path, window))


def _match_label_fields(
    svg: SvgArtifact, chart: ChartIR
) -> tuple[dict[str, tuple[SvgElement, ...]], list[str]]:
    matched: dict[str, tuple[SvgElement, ...]] = {}
    excluded: list[str] = []
    for path, field in _label_fields(chart):
        try:
            matched[path] = _verbatim(svg, field.text, field.evidence)
        except FigureError as error:
            excluded.append(f"{path}:{error}")
    return matched, excluded


def _project_point(point: ChartPoint, match: _PointMatch, receipt: _Receipt) -> ChartPoint:
    """One point, four fields, each carrying only the window that printed it."""
    origin = f"points.{point.point_id}"
    series = _blank(match.value) if match.series is None else _field(match.series)
    unit = (
        _blank(match.value)
        if match.unit is None
        # Verbatim inside the matched window, which may print it as ``VONB ($m)``.
        else TextField(
            fold_whitespace(point.unit.text),
            Evidence(_ids(match.unit), Verification.VERIFIED, _CONFIDENCE_POINTS),
        )
    )
    receipt.record(
        f"{origin}.series", match.series or match.value, printed=match.series is not None
    )
    receipt.record(f"{origin}.category", match.category)
    receipt.record(f"{origin}.unit", match.unit or match.value, printed=match.unit is not None)
    receipt.record(f"{origin}.value", match.value)
    return ChartPoint(
        point.point_id,
        series,
        _field(match.category),
        unit,
        NumericObservation(
            match.number,
            ValueKind.EXPLICIT,
            Evidence(_ids(match.value), Verification.VERIFIED, _CONFIDENCE_POINTS),
        ),
    )


def _project_chart(
    svg: SvgArtifact,
    chart: ChartIR,
    matched: dict[str, tuple[SvgElement, ...]],
    receipt: _Receipt,
) -> tuple[ChartIR, list[str]]:
    """Keep every printed surface; drop every unprinted one. Point by point, never figure-wide."""
    pool = _axis_citations(chart)
    points: list[ChartPoint] = []
    excluded: list[str] = []
    seen: set[str] = set()
    for point in chart.points:
        origin = f"points.{point.point_id}"
        if point.point_id in seen:
            excluded.append(f"{origin}:duplicate_point_id")
            continue
        seen.add(point.point_id)
        try:
            match = _match_point(svg, point, pool)
        except FigureError as error:
            excluded.append(f"{origin}:{error}")
            continue
        points.append(_project_point(point, match, receipt))
    axes: list[ChartAxis] = []
    for index, axis in enumerate(chart.axes):
        label = matched.get(f"axes.{index}.label")
        if label is None:
            continue
        caption = matched.get(f"axes.{index}.unit")
        position = len(axes)
        axes.append(
            ChartAxis(
                axis.axis_id,
                _field(label),
                _blank(label) if caption is None else _field(caption),
                axis.scale,
            )
        )
        receipt.record(f"axes.{position}.label", label)
        if caption is not None:
            receipt.record(f"axes.{position}.unit", caption)
    for name in ("title", "period"):
        window = matched.get(name)
        if window is not None:
            receipt.record(name, window)
    return (
        replace(
            chart,
            axes=tuple(axes),
            points=tuple(points),
            title=None if "title" not in matched else _field(matched["title"]),
            period=None if "period" not in matched else _field(matched["period"]),
            marks=(),
            producer=_POINTS_PRODUCER + chart.producer,
            verification=Verification.PENDING,
        ),
        excluded,
    )


def _project_labels(
    svg: SvgArtifact,
    chart: ChartIR,
    description: TextDescription,
    matched: dict[str, tuple[SvgElement, ...]],
    receipt: _Receipt,
) -> tuple[list[DescriptionClaim], list[str], list[str]]:
    """Both branches may name a label; one source window is indexed at most once per object."""
    claims: list[DescriptionClaim] = []
    from_claims: list[str] = []
    from_fields: list[str] = []
    used_elements: set[str] = set()
    used_windows: set[frozenset[str]] = set()

    def project(origin: str, window: tuple[SvgElement, ...]) -> bool:
        ids = frozenset(_ids(window))
        if ids in used_windows or ids & used_elements:
            return False
        used_windows.add(ids)
        used_elements.update(ids)
        receipt.record(f"claims.{len(claims)}.text", window, origin=origin)
        claims.append(
            DescriptionClaim(
                window_text(window),
                Evidence(_ids(window), Verification.VERIFIED, _CONFIDENCE_POINTS),
            )
        )
        return True

    for index, claim in enumerate(description.claims):
        origin = f"claims[{index}]"
        if claim.value is not None:
            from_claims.append(f"{origin}:numeric_claim_not_a_label")
            continue
        try:
            window = _verbatim(svg, claim.text, claim.evidence)
        except FigureError as error:
            from_claims.append(f"{origin}:{error}")
            continue
        if not project(origin, window):
            from_claims.append(f"{origin}:duplicate_label_occurrence")
    for path, _ in _label_fields(chart):
        printed = matched.get(path)
        if printed is not None and not project(path, printed):
            from_fields.append(f"{path}:duplicate_label_occurrence")
    return claims, from_claims, from_fields


def _project_points(svg: SvgArtifact, chart: ChartIR, description: TextDescription) -> _Projection:
    matched, unprinted_fields = _match_label_fields(svg, chart)
    labels, chart_rows = _Receipt(), _Receipt()
    claims, from_claims, duplicate_fields = _project_labels(
        svg, chart, description, matched, labels
    )
    projected, dropped_points = _project_chart(svg, chart, matched, chart_rows)
    return _Projection(
        projected,
        tuple(claims),
        tuple(labels.fields + chart_rows.fields),
        tuple(labels.geometry + chart_rows.geometry),
        tuple(from_claims + unprinted_fields + duplicate_fields + dropped_points),
        _METHOD_POINTS,
    )


def qualify_source_labels(
    svg: SvgArtifact,
    chart: ChartIR,
    description: TextDescription,
    *,
    scope: str = FIGURE_POINT_SCOPE,
) -> QualifiedLabelProjection:
    """Project only what this figure prints, under one declared policy.

    ``scope`` selects the policy and is recorded in the receipt, so a published snapshot
    re-derives under the very policy that qualified it. See the module docstring for what
    each policy admits — and, for the newer one, for the association it does not prove.
    """
    if scope not in FIGURE_LABEL_SCOPES:
        raise _fail("unknown_label_qualification_scope")
    if Verification.REJECTED in (
        svg.verification,
        chart.verification,
        description.verification,
    ):
        raise _fail("rejected_input_cannot_be_qualified")
    if (
        chart.binding != svg.binding
        or description.binding != svg.binding
        or chart.execution_mode is not ExecutionMode.PRODUCTION
        or description.execution_mode is not ExecutionMode.PRODUCTION
        or chart.verification is not Verification.PENDING
        or description.verification is not Verification.PENDING
    ):
        raise _fail("branch_binding_or_raw_status_mismatch")
    validate_svg(svg)
    projection = (
        _project_v1(svg, chart, description)
        if scope == FIGURE_LABEL_SCOPE
        else _project_points(svg, chart, description)
    )
    if not projection.claims:
        raise _fail("no_exact_source_labels")
    qualified = replace(
        description,
        claims=projection.claims,
        producer="qualified-source-labels-v1:" + description.producer,
        verification=Verification.VERIFIED,
    )
    receipt = FigureQualification(
        svg.binding,
        svg.source,
        projection.fields,
        projection.method,
        ExecutionMode.PRODUCTION,
        projection.geometry,
        scope,
    )
    return QualifiedLabelProjection(
        projection.chart,
        qualified,
        receipt,
        chart.artifact_id,
        description.artifact_id,
        projection.excluded,
    )
