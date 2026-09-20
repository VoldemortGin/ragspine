"""Field-scoped direct percent labels; no bar-height or change interpretation."""

from dataclasses import dataclass, replace
from hashlib import sha256

from enterprise_pdf_rag.adapters.bar_geometry import (
    DirectBarPoint,
    match_visible_bar_labels,
)
from enterprise_pdf_rag.adapters.donut_qualification import _full_spans
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure
from enterprise_pdf_rag.adapters.source_paint_bar import SourcePaintBarProof
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DISPLAYED_BAR_SCOPE,
    PERIOD_RULE,
    PointPeriodInterpretation,
)
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    Evidence,
    ExecutionMode,
    FieldOccurrence,
    FigureQualification,
    SvgArtifact,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
    content_id,
)
from enterprise_pdf_rag.figures.validation import evidence_elements, validate_svg


@dataclass(frozen=True, slots=True)
class QualifiedBarProjection:
    chart: ChartIR
    description: TextDescription
    qualification: FigureQualification
    point_periods: tuple[PointPeriodInterpretation, ...]
    raw_chart_id: str
    raw_description_id: str
    included_claim_paths: tuple[str, ...]
    excluded_claim_paths: tuple[str, ...]


def _evidence(old: Evidence, *elements: SvgElement) -> Evidence:
    if old.verification is Verification.REJECTED:
        raise ValueError("rejected_bar_evidence")
    return Evidence(
        tuple(element.element_id for element in elements),
        Verification.VERIFIED,
        Confidence(None, "source-paint-bar-v3 and unique direct-label geometry"),
    )


def _field(svg: SvgArtifact, old: TextField, source: SvgElement) -> TextField:
    refs = evidence_elements(svg, old.evidence)
    if (
        old.text != source.text
        or any(element.source_span_id != source.source_span_id for element in refs)
        or source.element_id not in old.evidence.element_ids
    ):
        raise ValueError("bar_field_source_occurrence_mismatch")
    return replace(old, evidence=_evidence(old.evidence, source))


def _source_elements(
    svg: SvgArtifact, point: DirectBarPoint
) -> tuple[SvgElement, SvgElement, SvgElement, SvgElement]:
    if point.literal is None:
        raise ValueError("bar_value_unavailable")
    full = _full_spans(svg)
    category = next(
        element for element in full if element.source_span_id == point.category.span_id
    )
    literal = next(
        element for element in full if element.source_span_id == point.literal.span_id
    )
    fragments = tuple(
        element
        for element in svg.elements
        if element.source_span_id == literal.source_span_id
    )
    number = next(
        (element for element in fragments if element.text == literal.text[:-1]), None
    )
    unit = next((element for element in fragments if element.text == "%"), None)
    if number is None or unit is None:
        raise ValueError("bar_literal_substring_provenance_missing")
    return category, literal, number, unit


def qualify_displayed_bar(
    prepared: PreparedFigure,
    *,
    raw_chart: ChartIR,
    description: TextDescription,
    source_proof: SourcePaintBarProof,
) -> QualifiedBarProjection:
    """Caller replays the proof from pinned source; this function checks scope."""
    svg = prepared.svg
    validate_svg(svg)
    if (
        raw_chart.binding != svg.binding
        or description.binding != svg.binding
        or Verification.REJECTED
        in (svg.verification, raw_chart.verification, description.verification)
        or raw_chart.execution_mode is not ExecutionMode.PRODUCTION
        or description.execution_mode is not ExecutionMode.PRODUCTION
        or raw_chart.grammar != "bar"
        or raw_chart.period is not None
        or raw_chart.title is None
        or svg.source.rotation != 0
        or svg.source.coordinate_frame != "page-top-left-points"
        or svg.source.transform != (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    ):
        raise ValueError("unsupported_displayed_bar_binding_or_grammar")
    if (
        source_proof.source_sha256 != svg.source.document_sha256
        or source_proof.source_revision != svg.source.source_revision
        or source_proof.page_index != svg.source.page_index
        or source_proof.native_svg_digest != prepared.view.native_svg_digest
        or source_proof.crop_svg_digest != prepared.view.crop_svg_digest
        or source_proof.bbox != svg.source.bbox
        or source_proof.source_text_digest
        != sha256(repr(prepared.paint_text_spans).encode()).hexdigest()
        or source_proof.profile.source_sha256 != svg.source.document_sha256
        or source_proof.profile.page_index != svg.source.page_index
    ):
        raise ValueError("bar_source_proof_binding_mismatch")
    full = _full_spans(svg)
    local_ids = {element.source_span_id for element in full}
    geometry = match_visible_bar_labels(
        vectors=source_proof.vectors,
        glyphs=source_proof.glyphs,
        spans=tuple(
            span for span in prepared.paint_text_spans if span.span_id in local_ids
        ),
        region=svg.source.bbox,
    )
    titles = tuple(
        element
        for element in full
        if element.text == "Expense Ratio"
        and element.anchor.bbox[3]
        < min(point.bar.bbox[1] for point in geometry.geometry.points)
    )
    if len(titles) != 1:
        raise ValueError("expense_ratio_title_scope_ambiguous")
    title = titles[0]
    qualified_title = _field(svg, raw_chart.title, title)
    by_category = {point.category.text: point for point in geometry.geometry.points}
    if (
        len(raw_chart.points) != len(by_category)
        or {point.category.text for point in raw_chart.points} != set(by_category)
        or len({point.point_id for point in raw_chart.points}) != len(raw_chart.points)
    ):
        raise ValueError("bar_categories_not_one_to_one")
    points = []
    periods = []
    for raw in raw_chart.points:
        source = by_category[raw.category.text]
        if source.value is None:
            if raw.value.kind is not ValueKind.UNAVAILABLE:
                raise ValueError("unlabelled_bar_cannot_supply_a_numeric_value")
            continue
        category, literal, number, unit = _source_elements(svg, source)
        if (
            raw.value.kind is not ValueKind.EXPLICIT
            or raw.value.value != source.value
            or raw.value.value.as_tuple().exponent != source.value.as_tuple().exponent
        ):
            raise ValueError("bar_explicit_value_or_precision_mismatch")
        refs = evidence_elements(svg, raw.value.evidence)
        if (
            any(element.source_span_id != literal.source_span_id for element in refs)
            or number.element_id not in raw.value.evidence.element_ids
        ):
            raise ValueError("bar_numeric_source_occurrence_mismatch")
        point = ChartPoint(
            raw.point_id,
            _field(svg, raw.series, title),
            _field(svg, raw.category, category),
            _field(svg, raw.unit, unit),
            replace(raw.value, evidence=_evidence(raw.value.evidence, number)),
        )
        points.append(point)
        periods.append(
            PointPeriodInterpretation(
                svg.binding,
                raw_chart.artifact_id,
                point.point_id,
                f"points.{point.point_id}.category",
                point.category.text,
                point.category.evidence,
                PERIOD_RULE,
                Verification.VERIFIED,
                Confidence(
                    None,
                    "visible source category interpreted as a point-local half-year period",
                ),
            )
        )
    if not points:
        raise ValueError("no_explicit_bar_values")
    # Independent description fields + source geometry only. ChartIR is never
    # read to generate the indexed sentence or to repair a claim's semantics.
    claims = []
    included = []
    excluded = []
    covered = []
    for index, claim in enumerate(description.claims):
        claim_source = by_category.get(str(claim.category))
        if claim_source is None or claim_source.value is None:
            excluded.append(f"claims.{index}")
            continue
        category, literal, number, unit = _source_elements(svg, claim_source)
        if (
            (claim.series, claim.unit, claim.period, claim.value)
            != (title.text, "%", None, claim_source.value)
            or claim.value is None
            or claim.value.as_tuple().exponent != claim_source.value.as_tuple().exponent
            or claim.text
            != f"{claim.series} for {claim.category}: {claim.value} {claim.unit}."
        ):
            raise ValueError("description_contains_unqualified_bar_semantics")
        expected = (title, category, number, unit)
        if set(claim.evidence.element_ids) != {
            element.element_id for element in expected
        }:
            raise ValueError("bar_description_source_occurrence_mismatch")
        claims.append(replace(claim, evidence=_evidence(claim.evidence, *expected)))
        included.append(f"claims.{index}")
        covered.append(claim.category)
    if len(covered) != len(points) or set(covered) != {
        point.category.text for point in points
    }:
        raise ValueError("bar_description_explicit_coverage_incomplete")
    fields = [FieldOccurrence("title", qualified_title.evidence.element_ids)]
    for point in points:
        for name, evidence in (
            ("series", point.series.evidence),
            ("category", point.category.evidence),
            ("unit", point.unit.evidence),
            ("value", point.value.evidence),
        ):
            fields.append(
                FieldOccurrence(f"points.{point.point_id}.{name}", evidence.element_ids)
            )
    role_id = content_id(
        "bar-paint-roles-v1",
        (
            geometry.roles,
            tuple(
                (
                    point.bar.native_path_ref,
                    point.category.span_id,
                    point.literal.span_id if point.literal else None,
                )
                for point in geometry.geometry.points
            ),
        ),
    )
    receipt = FigureQualification(
        svg.binding,
        svg.source,
        tuple(fields),
        "source-paint-bar-v3/direct-percent-labels-v1",
        ExecutionMode.PRODUCTION,
        (source_proof.proof_id, role_id),
        DISPLAYED_BAR_SCOPE,
    )
    return QualifiedBarProjection(
        replace(
            raw_chart,
            axes=(),
            points=tuple(points),
            title=qualified_title,
            period=None,
            marks=(),
            verification=Verification.PENDING,
            producer="qualified-displayed-bar-v1:" + raw_chart.producer,
        ),
        replace(
            description,
            claims=tuple(claims),
            verification=Verification.VERIFIED,
            producer="qualified-independent-bar-description-v1:" + description.producer,
        ),
        receipt,
        tuple(periods),
        raw_chart.artifact_id,
        description.artifact_id,
        tuple(included),
        tuple(excluded),
    )
