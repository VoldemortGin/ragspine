"""A verbatim-points chart member is re-read point by point, not by the ADR 0008 closure."""

from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_POINT_SCOPE,
    QualifiedLabelProjection,
    qualify_source_labels,
)
from enterprise_pdf_rag.answers.models import AbstainReason, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.verify import _POINT_SCOPE, ClaimVerification, verify_claims
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    QueryFailure,
    QueryPin,
)
from enterprise_pdf_rag.figures.models import (
    FieldOccurrence,
    NumericObservation,
    SvgArtifact,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.context_builder import BlockKind, ContextBlock
from tests.enterprise_pdf_rag.adapters.test_donut_qualification import sample

MEMBER = "b" * 64
PIN = QueryPin("a" * 64, "c" * 64, MEMBER)


def _projection() -> tuple[SvgArtifact, QualifiedLabelProjection]:
    prepared, chart, raw = sample()
    return prepared.svg, qualify_source_labels(prepared.svg, chart, raw)


def _context() -> ChartContext:
    svg, projection = _projection()
    return ChartContext(
        PIN, "d" * 64, projection.chart, projection.description, projection.receipt, svg
    )


def _block() -> ContextBlock:
    return ContextBlock(
        PIN.snapshot_id,
        MEMBER,
        BlockKind.CHART,
        17,
        FIGURE_POINT_SCOPE,
        Verification.PENDING,
        "Distribution Mix",
    )


def _verify(context: ChartContext, *claims: ModelClaim) -> ClaimVerification:
    answer = ModelAnswer(abstain=False, abstain_reason=None, answer="", claims=claims)
    return verify_claims(answer, {MEMBER: _block()}, chart_evidence=lambda _: context)


def _claim(field_path: str, text: str, *, claim_id: str = "c1") -> ModelClaim:
    return ModelClaim(
        claim_id=claim_id,
        member_id=MEMBER,
        kind="chart_value",
        field_path=field_path,
        text=text,
        row=None,
        col=None,
        header=None,
    )


def test_the_scope_string_answers_re_read_is_the_one_the_adapter_writes() -> None:
    assert _POINT_SCOPE == FIGURE_POINT_SCOPE


def test_a_printed_point_verifies_against_its_own_receipt_rows() -> None:
    context = _context()

    verification = _verify(context, _claim("points.Agency.value", "72%"))

    (claim,) = verification.verified
    assert (claim.kind, claim.text, claim.value, claim.unit) == (
        ClaimKind.CHART_VALUE,
        "72%",
        Decimal("72"),
        "%",
    )
    assert {citation.field_path for citation in claim.citations} == {
        "points.Agency.value",
        "points.Agency.series",
        "points.Agency.category",
        "points.Agency.unit",
        "period",
    }
    value = claim.citations[0]
    assert value.field_path == "points.Agency.value" and value.quote == "72%"
    assert value.chart_citation is not None and value.chart_citation.occurrences


def test_a_chart_without_a_period_still_verifies_its_points() -> None:
    """``check_fields`` would refuse this closure; the per-point one does not need it."""
    context = _context()
    trimmed = replace(context, chart=replace(context.chart, title=None, period=None))

    verification = _verify(trimmed, _claim("points.Agency.value", "72%"))

    (claim,) = verification.verified
    assert {citation.field_path for citation in claim.citations} == {
        "points.Agency.value",
        "points.Agency.series",
        "points.Agency.category",
        "points.Agency.unit",
    }


def test_a_point_whose_receipt_row_is_missing_fails_the_evidence_check() -> None:
    context = _context()
    stripped = tuple(
        field for field in context.qualification.fields if field.field_path != "points.Agency.unit"
    )
    tampered = replace(context, qualification=replace(context.qualification, fields=stripped))

    with pytest.raises(ChartQueryError) as error:
        _verify(tampered, _claim("points.Agency.value", "72%"))

    assert error.value.code is QueryFailure.INVALID_EVIDENCE


def test_a_receipt_row_that_names_other_elements_fails_the_evidence_check() -> None:
    context = _context()
    other = next(
        field.element_ids
        for field in context.qualification.fields
        if field.field_path == "points.Partnerships.value"
    )
    swapped = tuple(
        FieldOccurrence(field.field_path, other)
        if field.field_path == "points.Agency.value"
        else field
        for field in context.qualification.fields
    )
    tampered = replace(context, qualification=replace(context.qualification, fields=swapped))

    with pytest.raises(ChartQueryError) as error:
        _verify(tampered, _claim("points.Agency.value", "72%"))

    assert error.value.code is QueryFailure.INVALID_EVIDENCE


def test_an_unavailable_value_abstains_before_any_display_is_read() -> None:
    context = _context()
    points = tuple(
        replace(
            point,
            value=NumericObservation(None, ValueKind.UNAVAILABLE, point.value.evidence),
        )
        if point.point_id == "Agency"
        else point
        for point in context.chart.points
    )
    blanked = replace(context, chart=replace(context.chart, points=points))

    verification = _verify(blanked, _claim("points.Agency.value", "72%"))

    (rejected,) = verification.rejected
    assert rejected.reason is AbstainReason.VALUE_UNAVAILABLE


def test_a_point_the_projection_dropped_is_unknown_not_guessed() -> None:
    context = _context()

    verification = _verify(context, _claim("points.Nowhere.value", "5%"))

    (rejected,) = verification.rejected
    assert rejected.reason is AbstainReason.UNKNOWN_POINT


def test_a_number_the_figure_does_not_print_is_not_in_evidence() -> None:
    context = _context()

    verification = _verify(context, _claim("points.Agency.value", "71%"))

    (rejected,) = verification.rejected
    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_the_same_member_relabelled_as_a_donut_takes_the_stricter_path() -> None:
    """Dispatch is on the recorded scope: nothing may enter ADR 0008 through this door."""
    context = _context()
    relabelled = replace(
        context,
        qualification=replace(context.qualification, semantic_scope="explicit-distribution-shares"),
    )

    verification = _verify(relabelled, _claim("points.Agency.value", "72%"))

    (rejected,) = verification.rejected
    assert rejected.reason is AbstainReason.UNQUALIFIED_MEMBER


def test_a_point_id_carrying_a_decimal_is_read_as_the_prompt_printed_it() -> None:
    """Whatever the block prints as a citable path the verifier must be able to read back.

    A chart's point ids are derived from what the figure prints, so a value in the label
    puts a decimal point inside the id: the pinned AIA release prints
    ``points.point-1h26-roe-17.5.value`` in the prompt. The path parser stopped the id at
    the first dot, so that printed path could never be cited — the claim was thrown out as
    `MODEL_OUTPUT_INVALID` before the point was even looked up (live `p01-roe-quote-en` /
    `p15-cache-repeat-en`, 2026-09-21).
    """
    context = _context()

    verification = _verify(context, _claim("points.point-1h26-roe-17.5.value", "17.5%"))

    (rejected,) = verification.rejected
    # The id is parsed; this chart simply has no such point, which is a different verdict.
    assert rejected.reason is AbstainReason.UNKNOWN_POINT
    assert rejected.detail != "chart claims cite points.<id>.value"
    # A path that is not a point value at all is still refused.
    (malformed,) = _verify(context, _claim("points.Agency.label", "72%")).rejected
    assert malformed.reason is AbstainReason.MODEL_OUTPUT_INVALID
