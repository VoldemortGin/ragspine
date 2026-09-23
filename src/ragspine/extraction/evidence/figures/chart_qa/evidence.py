"""Check exact qualified fields and produce source-occurrence citations."""

import re
from decimal import Decimal

from ragspine.extraction.evidence.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    FieldCitation,
    QueryFailure,
)
from ragspine.extraction.evidence.figures.models import (
    ChartPoint,
    Evidence,
    EvidenceKind,
    ExecutionMode,
    Verification,
)
from ragspine.extraction.evidence.figures.source_label_match import (
    fold_whitespace,
    read_printed_value,
)
from ragspine.extraction.evidence.figures.validation import (
    evidence_elements,
    validate_pair,
    validate_svg,
)


def check_context(context: ChartContext) -> None:
    chart, description, receipt, svg = (
        context.chart,
        context.description,
        context.qualification,
        context.svg,
    )
    if (
        chart.binding != svg.binding
        or description.binding != svg.binding
        or receipt.binding != svg.binding
        or receipt.source != svg.source
        or chart.execution_mode is not ExecutionMode.PRODUCTION
        or description.execution_mode is not ExecutionMode.PRODUCTION
        or receipt.execution_mode is not ExecutionMode.PRODUCTION
    ):
        raise ChartQueryError(QueryFailure.INVALID_EVIDENCE, "Chart evidence binding differs")
    validate_svg(svg)


def check_fields(context: ChartContext) -> None:
    chart, receipt = context.chart, context.qualification
    if (
        not receipt.source_geometry_refs
        or chart.title is None
        or chart.period is None
        or chart.axes
        or chart.marks
        or len({p.point_id for p in chart.points}) != len(chart.points)
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Qualified chart closure is incomplete"
        )
    fields: dict[str, Evidence] = {
        "title": chart.title.evidence,
        "period": chart.period.evidence,
    }
    for point in chart.points:
        fields.update(
            {
                f"points.{point.point_id}.series": point.series.evidence,
                f"points.{point.point_id}.category": point.category.evidence,
                f"points.{point.point_id}.unit": point.unit.evidence,
                f"points.{point.point_id}.value": point.value.evidence,
            }
        )
    proved = {field.field_path: field.element_ids for field in receipt.fields}
    if set(fields) != set(proved) or any(
        evidence.verification is not Verification.VERIFIED or evidence.element_ids != proved[path]
        for path, evidence in fields.items()
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE,
            "Receipt does not prove every exact chart field",
        )
    if any(
        c.evidence.verification is not Verification.VERIFIED for c in context.description.claims
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Description contains an unqualified claim"
        )
    validate_pair(context.svg, chart, context.description)


def citation(context: ChartContext, path: str, evidence: Evidence) -> FieldCitation:
    return FieldCitation(
        path,
        context.chart.artifact_id,
        context.svg.artifact_id,
        context.svg.digest,
        context.qualification.artifact_id,
        evidence_elements(context.svg, evidence),
    )


def source_display(context: ChartContext, point: ChartPoint) -> str:
    numbers = evidence_elements(context.svg, point.value.evidence)
    units = evidence_elements(context.svg, point.unit.evidence)
    if (
        len(numbers) != 1
        or len(units) != 1
        or (not numbers[0].source_span_id or numbers[0].source_span_id != units[0].source_span_id)
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE,
            "Percentage requires one exact source occurrence",
        )
    span_elements = tuple(
        element
        for element in context.svg.elements
        if element.source_span_id == numbers[0].source_span_id and element.text_range is not None
    )
    bounds = (
        min(element.text_range[0] for element in span_elements if element.text_range is not None),
        max(element.text_range[1] for element in span_elements if element.text_range is not None),
    )
    complete = tuple(element for element in span_elements if element.text_range == bounds)
    candidates = tuple(
        element
        for element in complete
        if len(complete) == 1
        and re.fullmatch(r"\s*[+]?[0-9]+(?:\.[0-9]+)?\s*%\s*", element.text)
        and Decimal(element.text.strip().removesuffix("%").strip()) == point.value.value
        and element.text_range is not None
        and all(
            fragment.text_range is not None
            and element.text_range[0]
            <= fragment.text_range[0]
            < fragment.text_range[1]
            <= element.text_range[1]
            for fragment in (*numbers, *units)
        )
    )
    if len(candidates) != 1:
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE,
            "Original percentage display is unavailable or ambiguous",
        )
    return candidates[0].text


def check_point_fields(context: ChartContext, point: ChartPoint) -> None:
    """The per-point closure of ``check_fields``, for a scope that keeps points selectively.

    ``check_fields`` is ADR 0008 donut closure: it demands a title, a period, no axes and a
    receipt row for *every* field of *every* point. A verbatim-points projection keeps only
    the points the figure prints, so the closure is asked of the one point being cited —
    each of its four fields VERIFIED and proved by its own receipt row, nothing inferred.
    """
    receipt = context.qualification
    prefix = f"points.{point.point_id}"
    fields = {
        f"{prefix}.series": point.series.evidence,
        f"{prefix}.category": point.category.evidence,
        f"{prefix}.unit": point.unit.evidence,
        f"{prefix}.value": point.value.evidence,
    }
    proved = {field.field_path: field.element_ids for field in receipt.fields}
    if (
        not receipt.source_geometry_refs
        or len({item.point_id for item in context.chart.points}) != len(context.chart.points)
        or any(
            evidence.verification is not Verification.VERIFIED
            or proved.get(path) != evidence.element_ids
            for path, evidence in fields.items()
        )
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Receipt does not prove this exact chart point"
        )
    if any(
        item.evidence.verification is not Verification.VERIFIED
        for item in context.description.claims
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Description contains an unqualified claim"
        )


def verbatim_display(context: ChartContext, point: ChartPoint) -> str:
    """How the figure itself prints this point's number, and its unit when printed with it.

    Unlike ``source_display`` the occurrences need not share one span: a value may be a run
    of adjacent occurrences and its unit may be printed beside the number or read out of an
    axis caption elsewhere in the figure. What does not relax: every cited occurrence must be
    a source text observation, and their concatenation must read as exactly this point's value.
    """
    value = point.value.value
    numbers = evidence_elements(context.svg, point.value.evidence)
    if (
        value is None
        or not numbers
        or any(
            element.evidence_kind is not EvidenceKind.SOURCE_TEXT_OBSERVATION for element in numbers
        )
    ):
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Chart value requires exact source occurrences"
        )
    printed = fold_whitespace(" ".join(element.text for element in numbers))
    unit = fold_whitespace(point.unit.text)
    reading = read_printed_value(printed, unit)
    if reading is None or reading[0] != value:
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Cited occurrences do not print this value"
        )
    if not unit or reading[1]:
        return printed
    units = evidence_elements(context.svg, point.unit.evidence)
    if units and fold_whitespace(" ".join(element.text for element in units)) == unit:
        # Joined the way ``processing.index_text`` projects a value, so the answer's display
        # and the indexed text are the same string: ``72%`` but ``937 $m``.
        separator = " " if any(character.isalnum() for character in unit) else ""
        return f"{printed}{separator}{unit}"
    # The unit prints inside a larger caption; the number's own display stays the number.
    return printed
