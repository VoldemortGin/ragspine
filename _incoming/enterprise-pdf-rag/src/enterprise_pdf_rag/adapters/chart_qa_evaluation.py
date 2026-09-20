"""Deterministic, offline evaluation for the frozen traceable ChartQA slice."""

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class GoldOccurrence(_StrictModel):
    source_span_id: str
    observation_id: str
    text: str
    start: int
    end: int
    bbox: tuple[float, float, float, float]

    @model_validator(mode="after")
    def validate_occurrence(self) -> "GoldOccurrence":
        if self.start < 0 or self.end <= self.start:
            raise ValueError("Gold occurrence requires a positive text range")
        if self.end - self.start != len(self.text):
            raise ValueError("Gold occurrence text differs from its Unicode range")
        if self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("Gold occurrence bbox must have positive area")
        return self


class GoldEvidence(_StrictModel):
    series: GoldOccurrence
    period: GoldOccurrence
    category: GoldOccurrence
    value: GoldOccurrence
    unit: GoldOccurrence


class GoldFact(_StrictModel):
    point_id: str
    category: str
    value: str
    raw_display: str
    value_kind: Literal["explicit"]
    evidence: GoldEvidence


class QueryPoint(_StrictModel):
    point_id: str
    category: str


class GoldRequest(_StrictModel):
    target: str
    operation: str
    series: str
    period: str
    unit: str
    points: tuple[QueryPoint, ...]


class GoldExpected(_StrictModel):
    business_status: Literal["answered", "abstained", "transport_error"]
    http_status: int
    value: str | None = None
    unit: str | None = None
    value_kind: str | None = None
    raw_display: str | None = None
    verification: Literal["verified"] | None = None
    confidence_score: str | None = None
    confidence_method: str | None = None
    fact_ids: tuple[str, ...] = ()
    reason_class: str | None = None


class GoldCase(_StrictModel):
    case_id: str
    case_class: Literal["positive", "hard_negative", "unsupported"]
    request: GoldRequest
    expected: GoldExpected


class GoldCorpus(_StrictModel):
    name: str
    document_sha256: str
    source_page_count: int
    selected_physical_pages: tuple[int, ...]


class GoldScope(_StrictModel):
    grammar: Literal["donut"]
    series: str
    period: str
    unit: Literal["%"]
    allowed_operations: tuple[Literal["lookup", "percentage_point_difference"], ...]
    qualification_required: str


class GoldFigure(_StrictModel):
    object_id: str
    region_id: str
    page_index: int
    physical_page_number: int
    bbox: tuple[float, float, float, float]
    coordinate_frame: str
    page_text_sidecar_sha256: str
    region_source_text_sha256: str
    native_crop_svg_sha256: str
    structured_svg_sha256: str
    source_render_sha256: str


class GoldReview(_StrictModel):
    method: str
    status: Literal["assistant_source_reviewed"]
    review_date: str
    authority_excludes: tuple[str, ...]
    notes: tuple[str, ...]


class GoldThresholds(_StrictModel):
    answer_precision: str
    positive_answer_coverage: str
    citation_exactness: str
    citation_coverage: str
    hard_negative_escape_rate: str


class GoldPreconditions(_StrictModel):
    minimum_positive_cases: int
    positive_answer_coverage_must_be_nonzero: bool
    thresholds_must_be_present: bool


class ChartQaGold(_StrictModel):
    schema_version: Literal["chart-qa-gold-v1"]
    corpus: GoldCorpus
    scope: GoldScope
    figure: GoldFigure
    review: GoldReview
    facts: tuple[GoldFact, ...]
    cases: tuple[GoldCase, ...]
    adversarial_response_mutations: tuple[str, ...]
    thresholds: GoldThresholds
    pass_preconditions: GoldPreconditions

    @model_validator(mode="after")
    def validate_fixture(self) -> "ChartQaGold":
        digests = (
            self.corpus.document_sha256,
            self.figure.page_text_sidecar_sha256,
            self.figure.region_source_text_sha256,
            self.figure.native_crop_svg_sha256,
            self.figure.structured_svg_sha256,
            self.figure.source_render_sha256,
        )
        if any(not _is_sha256(value) for value in digests):
            raise ValueError("Gold source identities must be SHA-256 digests")
        if self.figure.page_index + 1 != self.figure.physical_page_number:
            raise ValueError("Gold physical and zero-based page identities differ")
        if self.corpus.selected_physical_pages != tuple(range(1, 21)):
            raise ValueError("Gold must name every selected physical page 1 through 20")
        fact_ids = tuple(fact.point_id for fact in self.facts)
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(fact_ids)) != len(fact_ids) or len(set(case_ids)) != len(case_ids):
            raise ValueError("Gold IDs must be unique")
        facts = set(fact_ids)
        positives = tuple(case for case in self.cases if case.case_class == "positive")
        if len(positives) < self.pass_preconditions.minimum_positive_cases:
            raise ValueError("Gold has too few positive cases")
        for case in positives:
            if case.expected.business_status != "answered":
                raise ValueError("Positive gold cases must be answerable")
            if not case.expected.fact_ids or not set(case.expected.fact_ids) <= facts:
                raise ValueError("Positive case refers to an unknown fact")
            if (
                case.expected.verification != "verified"
                or "confidence_score" not in case.expected.model_fields_set
                or case.expected.confidence_score is not None
                or case.expected.confidence_method is None
                or not case.expected.confidence_method.strip()
            ):
                raise ValueError(
                    "Positive case requires verified output and unknown-confidence method"
                )
            _validate_positive_arithmetic(case, self.facts)
        for case in self.cases:
            if (
                case.case_class != "positive"
                and case.expected.business_status == "answered"
            ):
                raise ValueError("Negative or unsupported case cannot expect an answer")
            if case.case_class == "hard_negative" and _request_is_true(
                case.request, self.scope, self.facts
            ):
                raise ValueError("Hard-negative mutation is another true gold query")
        for value in self.thresholds.model_dump().values():
            _decimal(value)
        return self


class SourceAnchorDto(_StrictModel):
    source_revision: str
    document_sha256: str
    page_index: int
    bbox: tuple[float, float, float, float]
    coordinate_frame: str
    rotation: int
    transform: tuple[float, float, float, float, float, float]


class OccurrenceDto(_StrictModel):
    element_id: str
    text: str
    anchor: SourceAnchorDto
    evidence_kind: Literal["source_text_observation"]
    source_span_id: str
    text_range: tuple[int, int]


class CitationDto(_StrictModel):
    field_path: str
    chart_ir_artifact_id: str
    svg_artifact_id: str
    svg_digest: str
    qualification_id: str
    occurrences: tuple[OccurrenceDto, ...]


class InputClaimDto(_StrictModel):
    point_id: str
    series: str
    category: str
    period: str
    unit: str
    value: str
    value_kind: Literal["explicit"]
    raw_display: str
    citations: tuple[CitationDto, ...]


class ConfidenceDto(_StrictModel):
    score: str | None
    method: str


class AnswerDto(_StrictModel):
    value: str
    unit: str
    value_kind: str
    raw_display: str | None
    verification: str
    confidence: ConfidenceDto


class CalculationInputDto(_StrictModel):
    chart_ir_artifact_id: str
    field_path: str
    value: str


class QueryPinDto(_StrictModel):
    processing_id: str
    snapshot_id: str
    member_id: str


class CalculationReceiptDto(_StrictModel):
    receipt_id: str
    pin: QueryPinDto
    source_manifest_id: str
    qualification_id: str
    rule_version: str
    operation: str
    decimal_policy: str
    precision: int
    rounding: str
    inputs: tuple[CalculationInputDto, ...]
    output_value: str
    output_unit: str
    source_refs: tuple[SourceAnchorDto, ...]


class ResponseDto(_StrictModel):
    schema_version: Literal["chart-qa-v1"]
    processing_id: str
    snapshot_id: str
    member_id: str
    operation: str
    status: Literal["answered", "abstained"]
    answer: AnswerDto | None
    inputs: tuple[InputClaimDto, ...]
    calculation_receipt: CalculationReceiptDto | None
    refusal_reason: str | None

    @model_validator(mode="after")
    def validate_status_shape(self) -> "ResponseDto":
        if self.status == "answered":
            if self.answer is None or self.refusal_reason is not None:
                raise ValueError("Answered response requires only an answer")
        elif self.answer is not None or self.calculation_receipt is not None:
            raise ValueError("Abstained response cannot contain answer material")
        return self


class ObservedCase(_StrictModel):
    case_id: str
    http_status: int
    body: ResponseDto | None
    error_class: str | None = None


class Observations(_StrictModel):
    schema_version: Literal["chart-qa-observations-v1"]
    results: tuple[ObservedCase, ...]


class CaseEvaluation(_StrictModel):
    case_id: str
    case_class: str
    passed: bool
    answer_correct: bool
    observed_status: str
    diagnostics: tuple[str, ...]
    expected_citation_count: int
    correct_citation_count: int
    returned_citation_count: int


class EvaluationMetrics(_StrictModel):
    answer_precision: str | None
    positive_answer_coverage: str
    refusal_rate: str
    unanswerable_refusal_recall: str | None
    answerable_over_refusal_rate: str
    citation_exactness: str | None
    citation_coverage: str
    hard_negative_escape_rate: str
    transport_error_count: int


class EvaluationReport(_StrictModel):
    schema_version: Literal["chart-qa-evaluation-v1"] = "chart-qa-evaluation-v1"
    gold_sha256: str
    observation_sha256: str
    passed: bool
    preconditions_passed: bool
    metrics: EvaluationMetrics
    cases: tuple[CaseEvaluation, ...]
    diagnostics: tuple[str, ...]

    @property
    def canonical_json(self) -> bytes:
        return self.model_dump_json(exclude_none=False).encode()

    @property
    def report_id(self) -> str:
        return sha256(self.canonical_json).hexdigest()


def load_gold(payload: bytes) -> ChartQaGold:
    """Load a strict immutable gold fixture."""
    return ChartQaGold.model_validate_json(payload, strict=True, extra="forbid")


def evaluate_chart_qa(
    gold_payload: bytes, observations_payload: bytes
) -> EvaluationReport:
    """Evaluate captured HTTP outcomes without making service or model calls."""
    gold = load_gold(gold_payload)
    observations = Observations.model_validate_json(
        observations_payload, strict=True, extra="forbid"
    )
    observed_by_id: dict[str, ObservedCase] = {}
    duplicate_ids: set[str] = set()
    for captured in observations.results:
        if captured.case_id in observed_by_id:
            duplicate_ids.add(captured.case_id)
        observed_by_id[captured.case_id] = captured
    known_ids = {case.case_id for case in gold.cases}
    unexpected_ids = set(observed_by_id) - known_ids

    evaluations: list[CaseEvaluation] = []
    answered = 0
    correct_answers = 0
    abstained = 0
    transport_errors = 0
    positive_total = 0
    positive_correct = 0
    positive_abstained = 0
    unanswerable_business_total = 0
    unanswerable_refused = 0
    hard_negative_total = 0
    hard_negative_answered = 0
    expected_citations = 0
    correct_citations = 0
    returned_citations = 0
    positive_pins: set[tuple[str, str, str]] = set()

    facts = {fact.point_id: fact for fact in gold.facts}
    for case in gold.cases:
        observed = observed_by_id.get(case.case_id)
        if case.case_class == "positive":
            positive_total += 1
        elif case.case_class == "hard_negative":
            hard_negative_total += 1
        if case.expected.business_status == "abstained":
            unanswerable_business_total += 1
        if observed is None:
            evaluations.append(
                CaseEvaluation(
                    case_id=case.case_id,
                    case_class=case.case_class,
                    passed=False,
                    answer_correct=False,
                    observed_status="missing",
                    diagnostics=("missing_observation",),
                    expected_citation_count=_expected_citation_count(case),
                    correct_citation_count=0,
                    returned_citation_count=0,
                )
            )
            expected_citations += _expected_citation_count(case)
            continue
        result, citation_expected, citation_correct, citation_returned = _evaluate_case(
            gold, case, observed, facts
        )
        evaluations.append(result)
        expected_citations += citation_expected
        correct_citations += citation_correct
        returned_citations += citation_returned
        status = result.observed_status
        if status == "answered":
            answered += 1
            if result.answer_correct:
                correct_answers += 1
                if case.case_class == "positive":
                    positive_correct += 1
                    if observed.body is not None:
                        positive_pins.add(
                            (
                                observed.body.processing_id,
                                observed.body.snapshot_id,
                                observed.body.member_id,
                            )
                        )
            if case.case_class == "hard_negative":
                hard_negative_answered += 1
        elif status == "abstained":
            abstained += 1
            if case.case_class == "positive":
                positive_abstained += 1
            if case.expected.business_status == "abstained":
                unanswerable_refused += 1
        elif status == "transport_error":
            transport_errors += 1

    global_diagnostics: list[str] = []
    if duplicate_ids:
        global_diagnostics.append(
            "duplicate_case_ids:" + ",".join(sorted(duplicate_ids))
        )
    if unexpected_ids:
        global_diagnostics.append(
            "unexpected_case_ids:" + ",".join(sorted(unexpected_ids))
        )
    valid_positive_pin = len(positive_pins) == 1 and all(
        _is_sha256(value) for value in next(iter(positive_pins), ())
    )
    if not valid_positive_pin:
        global_diagnostics.append("positive_cases_do_not_share_one_valid_pin")
    metrics = EvaluationMetrics(
        answer_precision=_ratio(correct_answers, answered),
        positive_answer_coverage=_ratio_required(positive_correct, positive_total),
        refusal_rate=_ratio_required(abstained, len(gold.cases)),
        unanswerable_refusal_recall=_ratio(
            unanswerable_refused, unanswerable_business_total
        ),
        answerable_over_refusal_rate=_ratio_required(
            positive_abstained, positive_total
        ),
        citation_exactness=_ratio(correct_citations, returned_citations),
        citation_coverage=_ratio_required(correct_citations, expected_citations),
        hard_negative_escape_rate=_ratio_required(
            hard_negative_answered, hard_negative_total
        ),
        transport_error_count=transport_errors,
    )
    preconditions = (
        positive_total >= gold.pass_preconditions.minimum_positive_cases
        and positive_correct > 0
        and not duplicate_ids
        and not unexpected_ids
        and valid_positive_pin
    )
    if not preconditions:
        global_diagnostics.append("evaluation_preconditions_failed")
    passed = (
        preconditions
        and all(result.passed for result in evaluations)
        and _meets_thresholds(metrics, gold.thresholds)
    )
    if not _meets_thresholds(metrics, gold.thresholds):
        global_diagnostics.append("metric_thresholds_failed")
    return EvaluationReport(
        gold_sha256=sha256(gold_payload).hexdigest(),
        observation_sha256=sha256(observations_payload).hexdigest(),
        passed=passed,
        preconditions_passed=preconditions,
        metrics=metrics,
        cases=tuple(evaluations),
        diagnostics=tuple(global_diagnostics),
    )


def _evaluate_case(
    gold: ChartQaGold,
    case: GoldCase,
    observed: ObservedCase,
    facts: dict[str, GoldFact],
) -> tuple[CaseEvaluation, int, int, int]:
    diagnostics: list[str] = []
    expected_citation_count = _expected_citation_count(case)
    correct_citation_count = 0
    returned_citation_count = 0
    answer_correct = False
    expected_status = case.expected.business_status
    if observed.http_status != case.expected.http_status:
        diagnostics.append("wrong_http_status")
    if observed.body is None:
        status = "transport_error"
        if expected_status != "transport_error":
            diagnostics.append("unexpected_transport_error")
        if case.expected.reason_class != observed.error_class:
            diagnostics.append("wrong_error_class")
    else:
        status = observed.body.status
        if status != expected_status:
            diagnostics.append("wrong_business_status")
        if observed.body.operation != case.request.operation:
            diagnostics.append("wrong_operation")
        if status == "answered":
            answer = observed.body.answer
            if answer is None:
                diagnostics.append("missing_answer")
            else:
                for field_name in ("value", "unit", "value_kind", "raw_display"):
                    if getattr(answer, field_name) != getattr(
                        case.expected, field_name
                    ):
                        diagnostics.append("wrong_answer_" + field_name)
                if answer.verification != case.expected.verification:
                    diagnostics.append("wrong_answer_verification")
                if answer.confidence.score != case.expected.confidence_score:
                    diagnostics.append("wrong_answer_confidence_score")
                if answer.confidence.method != case.expected.confidence_method:
                    diagnostics.append("wrong_answer_confidence_method")
            answer_errors = tuple(diagnostics)
            citation_result = _evaluate_inputs(gold, case, observed.body, facts)
            correct_citation_count, returned_citation_count, input_errors = (
                citation_result
            )
            diagnostics.extend(input_errors)
            calculation_errors = _evaluate_calculation(gold, case, observed.body)
            diagnostics.extend(calculation_errors)
            answer_correct = not answer_errors and not calculation_errors
        elif case.expected.reason_class != observed.body.refusal_reason:
            diagnostics.append("wrong_refusal_reason")
    result = CaseEvaluation(
        case_id=case.case_id,
        case_class=case.case_class,
        passed=not diagnostics,
        answer_correct=answer_correct,
        observed_status=status,
        diagnostics=tuple(sorted(set(diagnostics))),
        expected_citation_count=expected_citation_count,
        correct_citation_count=correct_citation_count,
        returned_citation_count=returned_citation_count,
    )
    return (
        result,
        expected_citation_count,
        correct_citation_count,
        returned_citation_count,
    )


def _evaluate_inputs(
    gold: ChartQaGold,
    case: GoldCase,
    body: ResponseDto,
    facts: dict[str, GoldFact],
) -> tuple[int, int, tuple[str, ...]]:
    diagnostics: list[str] = []
    expected_ids = case.expected.fact_ids
    if tuple(claim.point_id for claim in body.inputs) != expected_ids:
        diagnostics.append("wrong_input_order_or_identity")
    correct = 0
    returned = sum(len(claim.citations) for claim in body.inputs)
    for claim, expected_id in zip(body.inputs, expected_ids, strict=False):
        fact = facts[expected_id]
        if (
            claim.series,
            claim.category,
            claim.period,
            claim.unit,
            claim.value,
            claim.value_kind,
            claim.raw_display,
        ) != (
            gold.scope.series,
            fact.category,
            gold.scope.period,
            gold.scope.unit,
            fact.value,
            fact.value_kind,
            fact.raw_display,
        ):
            diagnostics.append("wrong_input_claim:" + expected_id)
        expected = {
            "series": fact.evidence.series,
            "category": fact.evidence.category,
            "period": fact.evidence.period,
            "unit": fact.evidence.unit,
            "value": fact.evidence.value,
        }
        seen: set[str] = set()
        for citation in claim.citations:
            field_name = citation.field_path.rsplit(".", 1)[-1]
            occurrence = expected.get(field_name)
            if field_name in seen or occurrence is None:
                diagnostics.append("unexpected_or_duplicate_citation:" + field_name)
                continue
            seen.add(field_name)
            if _citation_matches(gold, citation, occurrence):
                correct += 1
            else:
                diagnostics.append("wrong_citation:" + expected_id + ":" + field_name)
        if seen != set(expected):
            diagnostics.append("missing_field_citation:" + expected_id)
    return correct, returned, tuple(diagnostics)


def _citation_matches(
    gold: ChartQaGold, citation: CitationDto, expected: GoldOccurrence
) -> bool:
    if (
        not citation.chart_ir_artifact_id
        or not citation.svg_artifact_id
        or not citation.qualification_id
        or citation.svg_digest != gold.figure.structured_svg_sha256
        or len(citation.occurrences) != 1
    ):
        return False
    occurrence = citation.occurrences[0]
    anchor = occurrence.anchor
    return (
        occurrence.element_id == expected.observation_id
        and occurrence.text == expected.text
        and occurrence.source_span_id == expected.source_span_id
        and occurrence.text_range == (expected.start, expected.end)
        and anchor.source_revision == gold.corpus.document_sha256
        and anchor.document_sha256 == gold.corpus.document_sha256
        and anchor.page_index == gold.figure.page_index
        and anchor.bbox == expected.bbox
        and anchor.coordinate_frame == gold.figure.coordinate_frame
        and anchor.rotation == 0
        and anchor.transform == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    )


def _evaluate_calculation(
    gold: ChartQaGold, case: GoldCase, body: ResponseDto
) -> tuple[str, ...]:
    if case.request.operation == "lookup":
        return () if body.calculation_receipt is None else ("unexpected_calculation",)
    receipt = body.calculation_receipt
    if receipt is None:
        return ("missing_calculation_receipt",)
    errors: list[str] = []
    expected = case.expected
    expected_field_paths = tuple(
        f"points.{point_id}.value" for point_id in expected.fact_ids
    )
    input_chart_ids = tuple(
        next(
            (
                citation.chart_ir_artifact_id
                for citation in claim.citations
                if citation.field_path.endswith(".value")
            ),
            "",
        )
        for claim in body.inputs
    )
    expected_sources: list[SourceAnchorDto] = []
    for claim in body.inputs:
        value_citations = tuple(
            citation
            for citation in claim.citations
            if citation.field_path.endswith(".value")
        )
        if len(value_citations) == 1 and len(value_citations[0].occurrences) == 1:
            expected_sources.append(value_citations[0].occurrences[0].anchor)
    qualification_ids = {
        citation.qualification_id
        for claim in body.inputs
        for citation in claim.citations
    }
    if (
        not receipt.receipt_id.startswith("chart-calculation-v1:")
        or receipt.pin
        != QueryPinDto(
            processing_id=body.processing_id,
            snapshot_id=body.snapshot_id,
            member_id=body.member_id,
        )
        or not _is_sha256(receipt.source_manifest_id)
        or qualification_ids != {receipt.qualification_id}
        or not receipt.rule_version
        or receipt.operation != case.request.operation
        or receipt.decimal_policy
        != "base-10 exact subtraction; max 28 fractional places; trap Inexact and Rounded; no quantization"
        or receipt.precision != 64
        or receipt.rounding != "ROUND_HALF_EVEN"
        or receipt.output_value != expected.value
        or receipt.output_unit != expected.unit
        or len(receipt.inputs) != len(expected.fact_ids)
        or receipt.source_refs != tuple(expected_sources)
    ):
        errors.append("wrong_calculation_receipt")
    if (
        tuple(item.value for item in receipt.inputs)
        != tuple(claim.value for claim in body.inputs)
        or tuple(item.field_path for item in receipt.inputs) != expected_field_paths
    ):
        errors.append("wrong_calculation_input_order")
    if tuple(item.chart_ir_artifact_id for item in receipt.inputs) != input_chart_ids:
        errors.append("wrong_calculation_artifact_lineage")
    if any(
        source.source_revision != gold.corpus.document_sha256
        or source.document_sha256 != gold.corpus.document_sha256
        or source.page_index != gold.figure.page_index
        or source.coordinate_frame != gold.figure.coordinate_frame
        for source in receipt.source_refs
    ):
        errors.append("wrong_calculation_source_ref")
    return tuple(errors)


def _expected_citation_count(case: GoldCase) -> int:
    return len(case.expected.fact_ids) * 5


def _validate_positive_arithmetic(case: GoldCase, facts: tuple[GoldFact, ...]) -> None:
    values = {fact.point_id: _decimal(fact.value) for fact in facts}
    expected = case.expected
    if case.request.operation == "lookup":
        if (
            len(expected.fact_ids) != 1
            or _decimal(expected.value) != values[expected.fact_ids[0]]
        ):
            raise ValueError("Lookup gold answer differs from its fact")
    elif case.request.operation == "percentage_point_difference":
        if len(expected.fact_ids) != 2:
            raise ValueError("Difference gold requires two ordered facts")
        actual = values[expected.fact_ids[0]] - values[expected.fact_ids[1]]
        if _decimal(expected.value) != actual or expected.unit != "percentage_points":
            raise ValueError("Difference gold has wrong arithmetic or unit")
    else:
        raise ValueError("Positive gold uses an unsupported operation")


def _request_is_true(
    request: GoldRequest, scope: GoldScope, facts: tuple[GoldFact, ...]
) -> bool:
    if request.target != "qualified_member":
        return False
    if (request.series, request.period, request.unit) != (
        scope.series,
        scope.period,
        scope.unit,
    ):
        return False
    by_id = {fact.point_id: fact for fact in facts}
    return all(
        point.point_id in by_id and by_id[point.point_id].category == point.category
        for point in request.points
    )


def _decimal(value: str | None) -> Decimal:
    if value is None:
        raise ValueError("Expected decimal is absent")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Expected decimal is invalid") from error
    if not result.is_finite():
        raise ValueError("Expected decimal must be finite")
    return result


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(Decimal(numerator) / Decimal(denominator))


def _ratio_required(numerator: int, denominator: int) -> str:
    result = _ratio(numerator, denominator)
    if result is None:
        raise ValueError("Required evaluation stratum is empty")
    return result


def _meets_thresholds(metrics: EvaluationMetrics, thresholds: GoldThresholds) -> bool:
    required = (
        (metrics.answer_precision, thresholds.answer_precision, "min"),
        (
            metrics.positive_answer_coverage,
            thresholds.positive_answer_coverage,
            "min",
        ),
        (metrics.citation_exactness, thresholds.citation_exactness, "min"),
        (metrics.citation_coverage, thresholds.citation_coverage, "min"),
        (
            metrics.hard_negative_escape_rate,
            thresholds.hard_negative_escape_rate,
            "max",
        ),
    )
    for observed, threshold, direction in required:
        if observed is None:
            return False
        if direction == "min" and _decimal(observed) < _decimal(threshold):
            return False
        if direction == "max" and _decimal(observed) > _decimal(threshold):
            return False
    return True


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


CHART_QA_REPORT_ADAPTER = TypeAdapter(EvaluationReport)


def write_evaluation_bundle(
    *, gold_path: Path, observations_path: Path, output_root: Path
) -> Path:
    """Evaluate explicit inputs and persist one immutable content-addressed bundle."""
    gold_payload = gold_path.read_bytes()
    observations_payload = observations_path.read_bytes()
    report = evaluate_chart_qa(gold_payload, observations_payload)
    target = output_root / "chart-qa-evaluations" / report.report_id
    target.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("gold.json", gold_payload),
        ("observations.json", observations_payload),
        ("report.json", report.canonical_json),
    ):
        _write_immutable(target / name, payload)
    return target


def read_evaluation(folder: Path) -> EvaluationReport:
    """Load one persisted report only after checking its three-file closure."""
    report_payload = (folder / "report.json").read_bytes()
    gold_payload = (folder / "gold.json").read_bytes()
    observations_payload = (folder / "observations.json").read_bytes()
    report = CHART_QA_REPORT_ADAPTER.validate_json(
        report_payload, strict=True, extra="forbid"
    )
    if (
        folder.name != sha256(report_payload).hexdigest()
        or folder.name != report.report_id
        or report.gold_sha256 != sha256(gold_payload).hexdigest()
        or report.observation_sha256 != sha256(observations_payload).hexdigest()
    ):
        raise ValueError("Evaluation folder does not close over its exact inputs")
    return report


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Evaluation artifact already differs: {path.name}")
        return
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError(
                    f"Evaluation artifact already differs: {path.name}"
                ) from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline evaluator using only caller-supplied paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    target = write_evaluation_bundle(
        gold_path=arguments.gold,
        observations_path=arguments.observations,
        output_root=arguments.output_root,
    )
    sys.stdout.write(f"{target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
