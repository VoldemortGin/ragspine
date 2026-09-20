"""Offline acceptance of v2 observations against source gold and separate pins."""

import argparse
import json
from collections.abc import Sequence
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from enterprise_pdf_rag.adapters.chart_qa_capture import (
    classify_query_error,
    write_capture_output,
)
from enterprise_pdf_rag.adapters.chart_qa_evaluation import (
    ConfidenceDto,
    GoldCase,
    GoldFact,
    GoldOccurrence,
    SourceAnchorDto,
)
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import (
    BarCaptureTarget,
    BarCaptureTargets,
    BarGold,
    BarObservations,
    BarObservedCase,
    BarReleaseBindings,
    BarResponseDto,
    RoleCitationDto,
    StrictModel,
    load_bar_gold,
)


class BarCaseEvaluation(StrictModel):
    case_id: str
    case_class: str
    expected_http_status: int
    observed_http_status: int | None
    expected_status: str
    expected_reason: str | None
    observed_reason: str | None = None
    passed: bool
    observed_status: str
    answer_correct: bool = False
    correct_citations: int = 0
    returned_citations: int = 0
    period_role_correct: bool = False
    page_context_correct: bool = False
    normalization_correct: bool = False
    diagnostics: tuple[str, ...] = ()


class BarEvaluationMetrics(StrictModel):
    answer_precision: str
    positive_answer_coverage: str
    citation_exactness: str
    citation_coverage: str
    period_role_exactness: str
    page_context_exactness: str
    normalization_provenance_exactness: str
    hard_negative_escape_rate: str


class BarStratumMetrics(StrictModel):
    expected_cases: int
    correct_cases: int
    unexpected_answers: int
    correct_response_rate: str
    escape_rate: str


class BarEvaluationStrata(StrictModel):
    business_refusals: BarStratumMetrics
    unsupported_requests: BarStratumMetrics
    source_and_pin_faults: BarStratumMetrics


class BarEvaluationReport(StrictModel):
    schema_version: Literal["chart-qa-v2-evaluation-v1"] = "chart-qa-v2-evaluation-v1"
    gold_sha256: str
    targets_sha256: str
    observations_sha256: str
    passed: bool
    metrics: BarEvaluationMetrics
    strata: BarEvaluationStrata
    cases: tuple[BarCaseEvaluation, ...]
    diagnostics: tuple[str, ...]

    @property
    def report_id(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


def query_payload(case: GoldCase, target: BarCaptureTarget) -> bytes:
    """Build only the requested selectors, never a value or verification assertion."""
    return json.dumps(
        {
            "schema_version": "chart-qa-v2",
            "kind": "chart",
            "processing_id": target.http.processing_id,
            "snapshot_id": target.http.snapshot_id,
            "member_id": target.http.member_id,
            **case.request.model_dump(exclude={"target"}),
        },
        separators=(",", ":"),
    ).encode()


def _anchor(gold: BarGold, bbox: tuple[float, float, float, float]) -> SourceAnchorDto:
    return SourceAnchorDto(
        document_sha256=gold.corpus.document_sha256,
        source_revision=gold.corpus.document_sha256,
        page_index=gold.figure.page_index,
        bbox=bbox,
        coordinate_frame=gold.figure.coordinate_frame,
        rotation=0,
        transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    )


def _unknown(confidence: ConfidenceDto, expected_method: str) -> bool:
    return confidence.score is None and confidence.method == expected_method


def _citation_correct(
    gold: BarGold,
    release: BarReleaseBindings,
    fact: GoldFact,
    role: str,
    observed: RoleCitationDto,
) -> bool:
    source: GoldOccurrence = getattr(fact.evidence, role)
    path = f"points.{fact.point_id}.{'category' if role == 'period' else role}"
    citation = observed.citation
    if (
        observed.role != role
        or observed.raw_chart_ir_artifact_id != release.raw_chart_ir_artifact_id
        or observed.raw_field_path != path
        or citation.field_path != path
        or citation.chart_ir_artifact_id != release.chart_ir_artifact_id
        or citation.svg_artifact_id != release.binding.svg_artifact_id
        or citation.svg_digest != gold.figure.structured_svg_sha256
        or citation.qualification_id != release.qualification_id
        or len(citation.occurrences) != 1
    ):
        return False
    occurrence = citation.occurrences[0]
    return (
        occurrence.element_id == source.observation_id
        and occurrence.source_span_id == source.source_span_id
        and occurrence.text == source.text
        and occurrence.text_range == (source.start, source.end)
        and occurrence.anchor == _anchor(gold, source.bbox)
    )


def _positive(
    gold: BarGold, case: GoldCase, body: BarResponseDto, release: BarReleaseBindings
) -> BarCaseEvaluation:
    fact = next(f for f in gold.facts if f.point_id == case.expected.fact_ids[0])
    answer = body.answer
    correct_answer = (
        answer is not None
        and answer.value == fact.value
        and answer.unit == "%"
        and answer.value_kind == "explicit"
        and answer.raw_display == fact.raw_display
        and answer.verification == "verified"
        and answer.confidence.score is None
        and answer.confidence.method == case.expected.confidence_method
        and body.refusal_reason is None
    )
    citations = 0
    returned = sum(len(item.citations) for item in body.inputs)
    period_ok = False
    if len(body.inputs) == 1:
        claim = body.inputs[0]
        correct_answer &= (
            claim.point_id == fact.point_id
            and claim.series == gold.scope.series
            and claim.category == fact.category
            and claim.period == fact.category
            and claim.unit == "%"
            and claim.value == fact.value
            and claim.value_kind == "explicit"
            and claim.raw_display == fact.raw_display
        )
        by_role = {field.role: field for field in claim.citations}
        if len(by_role) == len(claim.citations):
            citations = sum(
                role in by_role and _citation_correct(gold, release, fact, role, by_role[role])
                for role in ("series", "category", "period", "unit", "value")
            )
        period = claim.period_interpretation
        period_ok = (
            period.binding == release.binding
            and period.raw_chart_ir_artifact_id == release.raw_chart_ir_artifact_id
            and period.point_id == fact.point_id
            and period.raw_field_path == f"points.{fact.point_id}.category"
            and period.literal == fact.category
            and period.rule_version == gold.scope.period_rule
            and period.verification == "verified"
            and _unknown(period.confidence, release.period_confidence_method)
            and period.evidence.element_ids == (fact.evidence.category.observation_id,)
            and period.evidence.verification == "verified"
            and _unknown(period.evidence.confidence, release.period_evidence_confidence_method)
        )
    else:
        correct_answer = False
    context_ok = False
    if len(body.page_context) == 1:
        context = body.page_context[0]
        context_ok = (
            context.source_manifest_id == gold.source_manifest_id
            and context.source_text_sha256 == gold.figure.page_text_sidecar_sha256
            and context.source_span_id == gold.page_context.source_span_id
            and context.text == gold.page_context.text
            and context.source == _anchor(gold, gold.page_context.bbox)
            and context.text_range == gold.page_context.text_range
            and context.scope == "page_context"
            and context.verification == "verified"
            and _unknown(context.confidence, release.page_context_confidence_method)
        )
    normalization_ok = body.description_normalization == release.normalization
    checks = (
        (correct_answer, "answer_or_input_mismatch"),
        (citations == returned == 5, "field_citation_mismatch"),
        (period_ok, "point_period_interpretation_mismatch"),
        (context_ok, "page_context_mismatch"),
        (normalization_ok, "description_normalization_mismatch"),
    )
    return BarCaseEvaluation(
        case_id=case.case_id,
        case_class=case.case_class,
        expected_http_status=case.expected.http_status,
        observed_http_status=200,
        expected_status=case.expected.business_status,
        expected_reason=case.expected.reason_class,
        passed=all(check for check, _ in checks),
        observed_status=body.status,
        answer_correct=correct_answer,
        correct_citations=citations,
        returned_citations=returned,
        period_role_correct=period_ok,
        page_context_correct=context_ok,
        normalization_correct=normalization_ok,
        diagnostics=tuple(message for check, message in checks if not check),
    )


def _evaluate_case(
    gold: BarGold, case: GoldCase, target: BarCaptureTarget, observed: BarObservedCase
) -> BarCaseEvaluation:
    failure = BarCaseEvaluation(
        case_id=case.case_id,
        case_class=case.case_class,
        expected_http_status=case.expected.http_status,
        observed_http_status=observed.http_status,
        expected_status=case.expected.business_status,
        expected_reason=case.expected.reason_class,
        passed=False,
        observed_status="invalid",
    )
    if json.loads(observed.request_json) != json.loads(query_payload(case, target)):
        return failure.model_copy(update={"diagnostics": ("captured_request_mismatch",)})
    if observed.http_status != case.expected.http_status:
        status = (
            BarResponseDto.model_validate_json(
                observed.response_json, strict=True, extra="forbid"
            ).status
            if observed.http_status == 200
            else "transport_error"
        )
        return failure.model_copy(
            update={"observed_status": status, "diagnostics": ("http_status_mismatch",)}
        )
    if observed.http_status != 200:
        reason = classify_query_error(observed.http_status, observed.response_json.encode())
        passed = (
            case.expected.business_status == "transport_error"
            and reason == case.expected.reason_class
        )
        return failure.model_copy(
            update={
                "passed": passed,
                "observed_status": "transport_error",
                "observed_reason": reason,
                "diagnostics": () if passed else ("error_class_mismatch",),
            }
        )
    body = BarResponseDto.model_validate_json(observed.response_json, strict=True, extra="forbid")
    if (
        body.processing_id != target.http.processing_id
        or body.snapshot_id != target.http.snapshot_id
        or body.member_id != target.http.member_id
        or body.semantic_scope != "source_display_only"
        or body.operation != case.request.operation
    ):
        return failure.model_copy(
            update={
                "observed_status": body.status,
                "observed_reason": body.refusal_reason,
                "diagnostics": ("pin_or_scope_mismatch",),
            }
        )
    if case.case_class == "positive" and body.status == "answered":
        assert target.release is not None
        return _positive(gold, case, body, target.release)
    refused = (
        case.expected.business_status == "abstained"
        and body.status == "abstained"
        and body.refusal_reason == case.expected.reason_class
        and body.answer is None
        and not body.inputs
        and not body.page_context
        and body.description_normalization is None
    )
    return failure.model_copy(
        update={
            "passed": refused,
            "observed_status": body.status,
            "observed_reason": body.refusal_reason,
            "diagnostics": () if refused else ("unexpected_answer_or_refusal",),
        }
    )


def validate_bar_targets(gold: BarGold, targets: BarCaptureTargets) -> None:
    if set(targets.targets) != {case.request.target for case in gold.cases}:
        raise ValueError("Capture targets must exactly cover gold target names")
    for case in gold.cases:
        if case.case_class != "positive":
            continue
        release = targets.targets[case.request.target].release
        if release is None or (
            release.source_manifest_id != gold.source_manifest_id
            or release.binding.source_revision != gold.corpus.document_sha256
            or release.binding.svg_digest != gold.figure.structured_svg_sha256
            or release.normalization.raw_response_sha256 != gold.normalization.raw_response_sha256
            or release.normalization.original_typed_description_artifact_id
            != gold.normalization.original_typed_description_artifact_id
        ):
            raise ValueError(
                "Positive capture target must bind the independently reviewed source closure"
            )


def _ratio(numerator: int, denominator: int) -> str:
    with localcontext() as context:
        context.prec = 28
        return str(Decimal(numerator) / denominator) if denominator else "0"


def _stratum(results: tuple[BarCaseEvaluation, ...]) -> BarStratumMetrics:
    correct = sum(result.passed for result in results)
    answers = sum(result.observed_status == "answered" for result in results)
    return BarStratumMetrics(
        expected_cases=len(results),
        correct_cases=correct,
        unexpected_answers=answers,
        correct_response_rate=_ratio(correct, len(results)),
        escape_rate=_ratio(answers, len(results)),
    )


def evaluate_bar_chart_qa(
    gold_payload: bytes, targets_payload: bytes, observations_payload: bytes
) -> BarEvaluationReport:
    """Targets are independent source-requalified bindings, not response assertions."""
    gold = load_bar_gold(gold_payload)
    targets = BarCaptureTargets.model_validate_json(targets_payload, strict=True, extra="forbid")
    validate_bar_targets(gold, targets)
    observations = BarObservations.model_validate_json(
        observations_payload, strict=True, extra="forbid"
    )
    if observations.targets_sha256 != sha256(targets_payload).hexdigest():
        raise ValueError("Observation targets digest differs from publication targets")
    by_id = {observed.case_id: observed for observed in observations.results}
    diagnostics: list[str] = []
    if len(by_id) != len(observations.results):
        diagnostics.append("duplicate_observations")
    if set(by_id) != {case.case_id for case in gold.cases}:
        diagnostics.append("observation_case_coverage_mismatch")
    results: list[BarCaseEvaluation] = []
    for case in gold.cases:
        observed = by_id.get(case.case_id)
        result = BarCaseEvaluation(
            case_id=case.case_id,
            case_class=case.case_class,
            expected_http_status=case.expected.http_status,
            observed_http_status=None if observed is None else observed.http_status,
            expected_status=case.expected.business_status,
            expected_reason=case.expected.reason_class,
            passed=False,
            observed_status="missing",
            diagnostics=("missing_observation",),
        )
        if observed is not None:
            try:
                result = _evaluate_case(gold, case, targets.targets[case.request.target], observed)
            except (ValidationError, json.JSONDecodeError, ValueError):
                result = result.model_copy(
                    update={
                        "observed_status": "invalid",
                        "diagnostics": ("invalid_http_response",),
                    }
                )
        results.append(result)
    positives = {case.case_id for case in gold.cases if case.case_class == "positive"}
    correct = sum(result.answer_correct for result in results)
    citations = sum(result.correct_citations for result in results)
    negative_answers = sum(
        result.observed_status == "answered" and result.case_id not in positives
        for result in results
    )
    metrics = BarEvaluationMetrics(
        answer_precision=_ratio(
            correct, sum(result.observed_status == "answered" for result in results)
        ),
        positive_answer_coverage=_ratio(correct, len(positives)),
        citation_exactness=_ratio(citations, sum(result.returned_citations for result in results)),
        citation_coverage=_ratio(citations, len(positives) * 5),
        period_role_exactness=_ratio(
            sum(result.period_role_correct for result in results), len(positives)
        ),
        page_context_exactness=_ratio(
            sum(result.page_context_correct for result in results), len(positives)
        ),
        normalization_provenance_exactness=_ratio(
            sum(result.normalization_correct for result in results), len(positives)
        ),
        hard_negative_escape_rate=_ratio(negative_answers, len(gold.cases) - len(positives)),
    )
    thresholds_met = metrics.model_dump() == gold.thresholds.model_dump()
    return BarEvaluationReport(
        gold_sha256=sha256(gold_payload).hexdigest(),
        targets_sha256=sha256(targets_payload).hexdigest(),
        observations_sha256=sha256(observations_payload).hexdigest(),
        passed=not diagnostics and all(result.passed for result in results) and thresholds_met,
        metrics=metrics,
        strata=BarEvaluationStrata(
            business_refusals=_stratum(
                tuple(
                    result
                    for result in results
                    if result.expected_status == "abstained" and result.expected_http_status == 200
                )
            ),
            unsupported_requests=_stratum(
                tuple(result for result in results if result.expected_http_status == 422)
            ),
            source_and_pin_faults=_stratum(
                tuple(result for result in results if result.expected_http_status in (409, 503))
            ),
        ),
        cases=tuple(results),
        diagnostics=tuple(diagnostics),
    )


def write_bar_evaluation_bundle(
    output_root: Path,
    gold_payload: bytes,
    targets_payload: bytes,
    observations_payload: bytes,
) -> Path:
    report = evaluate_bar_chart_qa(gold_payload, targets_payload, observations_payload)
    folder = output_root / "chart-qa-v2-evaluations" / report.report_id
    for name, payload in (
        ("gold.json", gold_payload),
        ("targets.json", targets_payload),
        ("observations.json", observations_payload),
        ("report.json", report.model_dump_json().encode()),
    ):
        write_capture_output(folder / name, payload)
    read_bar_evaluation(folder)
    return folder


def read_bar_evaluation(
    folder: Path, *, processing_id: str | None = None, snapshot_id: str | None = None
) -> BarEvaluationReport:
    """Recheck an immutable four-file closure, without requests or reevaluation."""
    payload = (folder / "report.json").read_bytes()
    report = BarEvaluationReport.model_validate_json(payload, strict=True, extra="forbid")
    if sha256(payload).hexdigest() != folder.name or report.report_id != folder.name:
        raise ValueError("Evaluation report digest differs from its folder identity")
    for name, expected in (
        ("gold.json", report.gold_sha256),
        ("targets.json", report.targets_sha256),
        ("observations.json", report.observations_sha256),
    ):
        if sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Evaluation dependency digest differs")
    if (processing_id is None) != (snapshot_id is None):
        raise ValueError("Evaluation release requires both processing and snapshot pins")
    if processing_id is not None:
        gold = load_bar_gold((folder / "gold.json").read_bytes())
        targets = BarCaptureTargets.model_validate_json((folder / "targets.json").read_bytes())
        validate_bar_targets(gold, targets)
        for case in gold.cases:
            if case.case_class != "positive":
                continue
            target = targets.targets[case.request.target].http
            if (target.processing_id, target.snapshot_id) != (
                processing_id,
                snapshot_id,
            ):
                raise ValueError("Evaluation positive target belongs to another release")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    folder = write_bar_evaluation_bundle(
        args.output_root,
        args.gold.read_bytes(),
        args.targets.read_bytes(),
        args.observations.read_bytes(),
    )
    return 0 if read_bar_evaluation(folder).passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
