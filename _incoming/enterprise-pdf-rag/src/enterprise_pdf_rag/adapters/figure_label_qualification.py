"""Qualify exact figure labels without promoting chart semantics or numbers."""

import re
from dataclasses import dataclass, replace

from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Confidence,
    DescriptionClaim,
    Evidence,
    EvidenceKind,
    ExecutionMode,
    FailureCode,
    FieldOccurrence,
    FigureError,
    FigureQualification,
    SvgArtifact,
    SvgElement,
    TextDescription,
    Verification,
)
from enterprise_pdf_rag.figures.validation import validate_svg

FIGURE_LABEL_SCOPE = "figure-source-labels-only-v1"
_METHOD = "exact-single-source-occurrence-label-v1; no numeric or financial relations"
_NUMERIC_TEXT = re.compile(r"\d|[%$€£¥]")


@dataclass(frozen=True, slots=True)
class QualifiedLabelProjection:
    chart: ChartIR
    description: TextDescription
    receipt: FigureQualification
    raw_chart_id: str
    raw_description_id: str
    excluded_claim_paths: tuple[str, ...]


def _fail(reason: str) -> FigureError:
    return FigureError(FailureCode.UNVERIFIED, reason)


def _exact_label(svg: SvgArtifact, claim: DescriptionClaim) -> SvgElement:
    if claim.value is not None:
        raise _fail("numeric_claim_not_a_label")
    if _NUMERIC_TEXT.search(claim.text):
        raise _fail("numeric_text_not_a_label")
    ids = claim.evidence.element_ids
    if claim.evidence.verification is Verification.REJECTED or len(ids) != len(
        set(ids)
    ):
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


def qualify_source_labels(
    svg: SvgArtifact, chart: ChartIR, description: TextDescription
) -> QualifiedLabelProjection:
    """Project exact raw-description labels; ChartIR is retained only as lineage."""
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
    claims: list[DescriptionClaim] = []
    fields: list[FieldOccurrence] = []
    geometry: list[str] = []
    excluded: list[str] = []
    confidence = Confidence(
        None,
        "deterministic exact source occurrence label; no semantic inference",
    )
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
        evidence = Evidence((element.element_id,), Verification.VERIFIED, confidence)
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
        fields.append(
            FieldOccurrence(f"claims.{output_index}.text", (element.element_id,))
        )
        geometry.append(
            f"source-span:{element.source_span_id}:element:{element.element_id}"
        )
    if not claims:
        raise _fail("no_exact_source_labels")
    qualified = replace(
        description,
        claims=tuple(claims),
        producer="qualified-source-labels-v1:" + description.producer,
        verification=Verification.VERIFIED,
    )
    receipt = FigureQualification(
        svg.binding,
        svg.source,
        tuple(fields),
        _METHOD,
        ExecutionMode.PRODUCTION,
        tuple(geometry),
        FIGURE_LABEL_SCOPE,
    )
    masked_chart = replace(
        chart,
        axes=(),
        points=(),
        title=None,
        period=None,
        marks=(),
        producer="source-labels-only-unknown-chart-v1:" + chart.producer,
        verification=Verification.PENDING,
    )
    return QualifiedLabelProjection(
        masked_chart,
        qualified,
        receipt,
        chart.artifact_id,
        description.artifact_id,
        tuple(excluded),
    )
