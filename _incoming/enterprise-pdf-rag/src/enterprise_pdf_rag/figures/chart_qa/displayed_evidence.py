"""Defend the displayed-bar port's field, raw lineage and role contracts."""

import re

from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    PERIOD_RULE,
    DisplayedLookupContext,
)
from enterprise_pdf_rag.figures.chart_qa.evidence import check_context
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    QueryFailure,
)
from enterprise_pdf_rag.figures.models import (
    Evidence,
    ExecutionMode,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.figures.validation import validate_pair

ACTUAL_FX_CONTEXT = "Expense ratio comparatives and two-year changes are shown on an actual exchange rate basis"


def chart_context(context: DisplayedLookupContext) -> ChartContext:
    return ChartContext(
        context.pin,
        context.source_manifest_id,
        context.chart,
        context.description,
        context.qualification,
        context.svg,
    )


def _invalid(message: str) -> ChartQueryError:
    return ChartQueryError(QueryFailure.INVALID_EVIDENCE, message)


def _page_context(context: DisplayedLookupContext) -> None:
    if len(context.page_context) != 1:
        raise _invalid("Displayed lookup requires its separate actual-FX page context")
    note = context.page_context[0]
    source = context.svg.source
    if (
        note.scope != "page_context"
        or note.source_manifest_id != context.source_manifest_id
        or re.fullmatch(r"[0-9a-f]{64}", note.source_text_sha256) is None
        or not note.source_span_id
        or note.text != ACTUAL_FX_CONTEXT
        or note.text_range != (0, len(note.text))
        or note.verification is not Verification.VERIFIED
        or note.confidence.score is not None
        or (
            note.source.source_revision,
            note.source.document_sha256,
            note.source.page_index,
            note.source.coordinate_frame,
            note.source.rotation,
            note.source.transform,
        )
        != (
            source.source_revision,
            source.document_sha256,
            source.page_index,
            source.coordinate_frame,
            source.rotation,
            source.transform,
        )
        or any(
            item.source_span_id == note.source_span_id for item in context.svg.elements
        )
        or (
            max(note.source.bbox[0], source.bbox[0])
            < min(note.source.bbox[2], source.bbox[2])
            and max(note.source.bbox[1], source.bbox[1])
            < min(note.source.bbox[3], source.bbox[3])
        )
    ):
        raise _invalid(
            "Page context is unqualified, changed, or confused with crop evidence"
        )


def check_displayed_evidence(context: DisplayedLookupContext) -> None:
    check_context(chart_context(context))
    audit = context.description_normalization
    if (
        audit.binding != context.svg.binding
        or audit.rule_version != "duplicate-evidence-normalization-v1"
        or re.fullmatch(r"description-normalization-v1:[0-9a-f]{64}", audit.receipt_id)
        is None
        or re.fullmatch(r"[0-9a-f]{64}", audit.raw_response_sha256) is None
        or any(
            re.fullmatch(r"description-v2:[0-9a-f]{64}", value) is None
            for value in (
                audit.original_typed_description_artifact_id,
                audit.normalized_description_artifact_id,
            )
        )
        or audit.original_typed_description_artifact_id
        == audit.normalized_description_artifact_id
    ):
        raise _invalid(
            "Normalization audit has an unknown rule or incompatible branch identity"
        )
    chart, raw, receipt = context.chart, context.raw_chart, context.qualification
    if (
        raw.binding != context.svg.binding
        or raw.execution_mode is not ExecutionMode.PRODUCTION
        or raw.verification is not Verification.PENDING
        or chart.verification is not Verification.PENDING
        or chart.period is not None
        or raw.period is not None
        or chart.axes
        or chart.marks
        or not chart.points
        or chart.title is None
        or raw.title is None
        or chart.title.text != raw.title.text
        or not set(chart.title.evidence.element_ids).issubset(
            raw.title.evidence.element_ids
        )
        or not receipt.source_geometry_refs
        or len({p.point_id for p in raw.points}) != len(raw.points)
        or len({p.point_id for p in chart.points}) != len(chart.points)
    ):
        raise _invalid(
            "Displayed projection changed its raw binding or global chart semantics"
        )
    fields: dict[str, Evidence] = {"title": chart.title.evidence}
    for point in chart.points:
        original = next((p for p in raw.points if p.point_id == point.point_id), None)
        if (
            original is None
            or original.value.kind is not ValueKind.EXPLICIT
            or point.value.kind is not ValueKind.EXPLICIT
            or point.value.value != original.value.value
            or (point.series.text, point.category.text, point.unit.text)
            != (original.series.text, original.category.text, original.unit.text)
            or any(
                not set(projected.element_ids).issubset(old.element_ids)
                for projected, old in (
                    (point.series.evidence, original.series.evidence),
                    (point.category.evidence, original.category.evidence),
                    (point.unit.evidence, original.unit.evidence),
                    (point.value.evidence, original.value.evidence),
                )
            )
        ):
            raise _invalid("A displayed field differs from its explicit raw point")
        fields.update(
            {
                f"points.{point.point_id}.{name}": evidence
                for name, evidence in (
                    ("series", point.series.evidence),
                    ("category", point.category.evidence),
                    ("unit", point.unit.evidence),
                    ("value", point.value.evidence),
                )
            }
        )
    proved = {item.field_path: item.element_ids for item in receipt.fields}
    if set(fields) != set(proved) or any(
        evidence.verification is not Verification.VERIFIED
        or len(set(evidence.element_ids)) != len(evidence.element_ids)
        or evidence.element_ids != proved[path]
        for path, evidence in fields.items()
    ):
        raise _invalid(
            "Receipt does not bind every displayed field to its exact occurrence"
        )
    if len(context.point_periods) != len(chart.points) or {
        item.point_id for item in context.point_periods
    } != {p.point_id for p in chart.points}:
        raise _invalid(
            "Every qualified point requires exactly one period-role interpretation"
        )
    for role in context.point_periods:
        point = next(p for p in chart.points if p.point_id == role.point_id)
        if (
            role.binding != context.svg.binding
            or role.raw_chart_ir_artifact_id != raw.artifact_id
            or role.raw_field_path != f"points.{point.point_id}.category"
            or role.literal != point.category.text
            or role.evidence != point.category.evidence
            or role.rule_version != PERIOD_RULE
            or role.verification is not Verification.VERIFIED
            or role.confidence.score is not None
        ):
            raise _invalid(
                "Period role is not the exact qualified raw category occurrence"
            )
    if context.description.verification is not Verification.VERIFIED or any(
        claim.evidence.verification is not Verification.VERIFIED
        for claim in context.description.claims
    ):
        raise _invalid("Independent description contains an unqualified claim")
    validate_pair(context.svg, chart, context.description)
    _page_context(context)
