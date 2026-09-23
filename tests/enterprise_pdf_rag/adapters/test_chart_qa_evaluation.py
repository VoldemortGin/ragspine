"""Offline acceptance for the independently reviewed ChartQA gold slice."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from enterprise_pdf_rag.adapters.chart_qa_evaluation import (
    ChartQaGold,
    EvaluationReport,
    GoldOccurrence,
    ResponseDto,
    evaluate_chart_qa,
    load_gold,
    main,
    read_evaluation,
    write_evaluation_bundle,
)
from enterprise_pdf_rag.adapters.http.chart_qa_schemas import ChartQueryResponse
from ragspine.extraction.evidence.figures.chart_qa.models import Operation, PointSelector
from ragspine.extraction.evidence.figures.chart_qa.service import ChartQAService
from tests.enterprise_pdf_rag.figures.test_chart_qa import (
    PinnedResolver,
    qualified_context,
    question,
)

ROOT = Path(__file__).parents[3]
GOLD_PATH = ROOT / "data/benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-gold-v1.json"
PROCESSING_ID = "1" * 64
SNAPSHOT_ID = "2" * 64
MEMBER_ID = "3" * 64


def _anchor(gold: ChartQaGold, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    return {
        "source_revision": gold.corpus.document_sha256,
        "document_sha256": gold.corpus.document_sha256,
        "page_index": gold.figure.page_index,
        "bbox": list(bbox),
        "coordinate_frame": gold.figure.coordinate_frame,
        "rotation": 0,
        "transform": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
    }


def _citation(
    gold: ChartQaGold,
    point_id: str,
    field_name: str,
    occurrence: GoldOccurrence,
) -> dict[str, Any]:
    return {
        "field_path": "period" if field_name == "period" else f"points.{point_id}.{field_name}",
        "chart_ir_artifact_id": "chart-ir-v1:test",
        "svg_artifact_id": "svg-v2:test",
        "svg_digest": gold.figure.structured_svg_sha256,
        "qualification_id": "qualification-v1:test",
        "occurrences": [
            {
                "element_id": occurrence.observation_id,
                "text": occurrence.text,
                "anchor": _anchor(gold, occurrence.bbox),
                "evidence_kind": "source_text_observation",
                "source_span_id": occurrence.source_span_id,
                "text_range": [occurrence.start, occurrence.end],
            }
        ],
    }


def _input(gold: ChartQaGold, point_id: str) -> dict[str, Any]:
    fact = next(item for item in gold.facts if item.point_id == point_id)
    citations = [
        _citation(gold, point_id, name, getattr(fact.evidence, name))
        for name in ("series", "category", "period", "unit", "value")
    ]
    return {
        "point_id": fact.point_id,
        "series": gold.scope.series,
        "category": fact.category,
        "period": gold.scope.period,
        "unit": gold.scope.unit,
        "value": fact.value,
        "value_kind": fact.value_kind,
        "raw_display": fact.raw_display,
        "citations": citations,
    }


def _body(gold: ChartQaGold, case_id: str) -> dict[str, Any]:
    case = next(item for item in gold.cases if item.case_id == case_id)
    if case.expected.business_status == "abstained":
        return {
            "schema_version": "chart-qa-v1",
            "processing_id": PROCESSING_ID,
            "snapshot_id": SNAPSHOT_ID,
            "member_id": MEMBER_ID,
            "operation": case.request.operation,
            "status": "abstained",
            "answer": None,
            "inputs": [],
            "calculation_receipt": None,
            "refusal_reason": case.expected.reason_class,
        }
    inputs = [_input(gold, point_id) for point_id in case.expected.fact_ids]
    receipt = None
    if case.request.operation == "percentage_point_difference":
        source_refs: list[dict[str, Any]] = []
        for item in inputs:
            citation = next(
                entry for entry in item["citations"] if entry["field_path"].endswith(".value")
            )
            source_refs.append(citation["occurrences"][0]["anchor"])
        receipt = {
            "receipt_id": "chart-calculation-v1:test",
            "pin": {
                "processing_id": PROCESSING_ID,
                "snapshot_id": SNAPSHOT_ID,
                "member_id": MEMBER_ID,
            },
            "source_manifest_id": "4" * 64,
            "qualification_id": "qualification-v1:test",
            "rule_version": "percentage-point-difference-v1",
            "operation": case.request.operation,
            "decimal_policy": "base-10 exact subtraction; max 28 fractional places; trap Inexact and Rounded; no quantization",
            "precision": 64,
            "rounding": "ROUND_HALF_EVEN",
            "inputs": [
                {
                    "chart_ir_artifact_id": "chart-ir-v1:test",
                    "field_path": f"points.{item['point_id']}.value",
                    "value": item["value"],
                }
                for item in inputs
            ],
            "output_value": case.expected.value,
            "output_unit": case.expected.unit,
            "source_refs": source_refs,
        }
    return {
        "schema_version": "chart-qa-v1",
        "processing_id": PROCESSING_ID,
        "snapshot_id": SNAPSHOT_ID,
        "member_id": MEMBER_ID,
        "operation": case.request.operation,
        "status": "answered",
        "answer": {
            "value": case.expected.value,
            "unit": case.expected.unit,
            "value_kind": case.expected.value_kind,
            "raw_display": case.expected.raw_display,
            "verification": case.expected.verification,
            "confidence": {
                "score": case.expected.confidence_score,
                "method": case.expected.confidence_method,
            },
        },
        "inputs": inputs,
        "calculation_receipt": receipt,
        "refusal_reason": None,
    }


def _perfect_observations(gold: ChartQaGold) -> dict[str, Any]:
    results = []
    for case in gold.cases:
        if case.expected.business_status == "transport_error":
            results.append(
                {
                    "case_id": case.case_id,
                    "http_status": case.expected.http_status,
                    "body": None,
                    "error_class": case.expected.reason_class,
                }
            )
        else:
            results.append(
                {
                    "case_id": case.case_id,
                    "http_status": 200,
                    "body": _body(gold, case.case_id),
                    "error_class": None,
                }
            )
    return {"schema_version": "chart-qa-observations-v1", "results": results}


def _evaluate(observations: dict[str, Any]) -> EvaluationReport:
    return evaluate_chart_qa(
        GOLD_PATH.read_bytes(),
        json.dumps(observations, sort_keys=True, separators=(",", ":")).encode(),
    )


def test_reviewed_gold_and_perfect_results_pass_all_narrow_scope_gates() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())

    report = _evaluate(_perfect_observations(gold))

    assert report.passed
    assert report.preconditions_passed
    assert report.metrics.answer_precision == "1"
    assert report.metrics.positive_answer_coverage == "1"
    assert report.metrics.citation_exactness == "1"
    assert report.metrics.citation_coverage == "1"
    assert report.metrics.hard_negative_escape_rate == "0"
    assert len(report.report_id) == 64


def test_production_response_dto_is_accepted_by_evaluator_contract() -> None:
    context = qualified_context()
    query = replace(
        question(context),
        operation=Operation.PERCENTAGE_POINT_DIFFERENCE,
        points=tuple(
            PointSelector(point.point_id, point.category.text) for point in context.chart.points
        ),
    )
    domain = ChartQAService(PinnedResolver(context)).answer(query)
    payload = ChartQueryResponse.from_domain(domain).model_dump_json().encode()

    parsed = ResponseDto.model_validate_json(payload, strict=True, extra="forbid")

    assert parsed.calculation_receipt is not None
    assert len(parsed.calculation_receipt.source_refs) == 2
    assert tuple(item.value for item in parsed.calculation_receipt.inputs) == (
        "72",
        "28",
    )


def test_all_refusals_fail_positive_coverage_instead_of_faking_precision() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    for result in observations["results"]:
        if result["case_id"] in {
            "lookup-agency-72",
            "lookup-partnerships-28",
            "difference-agency-minus-partnerships",
            "difference-partnerships-minus-agency",
        }:
            result["body"] = {
                **_body(gold, "wrong-category"),
                "operation": next(
                    item.request.operation
                    for item in gold.cases
                    if item.case_id == result["case_id"]
                ),
            }

    report = _evaluate(observations)

    assert not report.passed
    assert not report.preconditions_passed
    assert report.metrics.answer_precision is None
    assert report.metrics.positive_answer_coverage == "0"
    assert report.metrics.answerable_over_refusal_rate == "1"


def test_wrong_number_and_wrong_source_occurrence_fail_separate_metrics() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    result = next(item for item in observations["results"] if item["case_id"] == "lookup-agency-72")
    result["body"]["answer"]["value"] = "28"
    value_citation = next(
        item
        for item in result["body"]["inputs"][0]["citations"]
        if item["field_path"].endswith(".value")
    )
    value_citation["occurrences"][0]["source_span_id"] = "span-v1-wrong"

    report = _evaluate(observations)
    case = next(item for item in report.cases if item.case_id == "lookup-agency-72")

    assert not report.passed
    assert "wrong_answer_value" in case.diagnostics
    assert "wrong_citation:point-agency:value" in case.diagnostics
    assert report.metrics.answer_precision == "0.75"
    assert report.metrics.positive_answer_coverage == "0.75"
    assert report.metrics.citation_exactness != "1"
    assert report.metrics.citation_coverage != "1"


def test_bad_citation_fails_citation_gate_without_rewriting_answer_precision() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    result = next(
        item for item in observations["results"] if item["case_id"] == "lookup-partnerships-28"
    )
    result["body"]["inputs"][0]["citations"][0]["svg_digest"] = "0" * 64

    report = _evaluate(observations)

    assert not report.passed
    assert report.metrics.answer_precision == "1"
    assert report.metrics.positive_answer_coverage == "1"
    assert report.metrics.citation_exactness != "1"


def test_model_style_confidence_cannot_claim_verified_answer_quality() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    result = next(item for item in observations["results"] if item["case_id"] == "lookup-agency-72")
    result["body"]["answer"]["confidence"] = {
        "score": None,
        "method": "model-self-assessment",
    }

    report = _evaluate(observations)
    case = next(item for item in report.cases if item.case_id == "lookup-agency-72")

    assert not report.passed
    assert "wrong_answer_confidence_method" in case.diagnostics
    assert report.metrics.answer_precision == "0.75"


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    (
        ("verification", "pending", "wrong_answer_verification"),
        ("confidence_score", "1", "wrong_answer_confidence_score"),
    ),
)
def test_answer_status_cannot_substitute_for_verified_unknown_confidence(
    field: str, value: str, diagnostic: str
) -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    result = next(item for item in observations["results"] if item["case_id"] == "lookup-agency-72")
    if field == "confidence_score":
        result["body"]["answer"]["confidence"]["score"] = value
    else:
        result["body"]["answer"][field] = value

    report = _evaluate(observations)
    case = next(item for item in report.cases if item.case_id == "lookup-agency-72")

    assert not report.passed
    assert diagnostic in case.diagnostics


def test_difference_receipt_must_preserve_operand_order_even_if_output_matches() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    result = next(
        item
        for item in observations["results"]
        if item["case_id"] == "difference-agency-minus-partnerships"
    )
    result["body"]["calculation_receipt"]["inputs"].reverse()

    report = _evaluate(observations)
    case = next(
        item for item in report.cases if item.case_id == "difference-agency-minus-partnerships"
    )

    assert not report.passed
    assert "wrong_calculation_input_order" in case.diagnostics


def test_missing_case_and_unknown_extra_case_fail_report_preconditions() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    observations = _perfect_observations(gold)
    observations["results"].pop()
    extra = deepcopy(observations["results"][0])
    extra["case_id"] = "not-in-gold"
    observations["results"].append(extra)

    report = _evaluate(observations)

    assert not report.passed
    assert not report.preconditions_passed
    assert any(item.startswith("unexpected_case_ids:") for item in report.diagnostics)
    assert any(item.observed_status == "missing" for item in report.cases)


def test_explicit_paths_write_one_immutable_content_addressed_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gold_payload = GOLD_PATH.read_bytes()
    gold = load_gold(gold_payload)
    observations_payload = json.dumps(_perfect_observations(gold), separators=(",", ":")).encode()
    gold_path = tmp_path / "inputs" / "gold.json"
    observations_path = tmp_path / "inputs" / "observations.json"
    gold_path.parent.mkdir()
    gold_path.write_bytes(gold_payload)
    observations_path.write_bytes(observations_payload)
    output_root = tmp_path / "run"

    target = write_evaluation_bundle(
        gold_path=gold_path,
        observations_path=observations_path,
        output_root=output_root,
    )

    report = evaluate_chart_qa(gold_payload, observations_payload)
    assert target == output_root / "chart-qa-evaluations" / report.report_id
    assert (target / "gold.json").read_bytes() == gold_payload
    assert (target / "observations.json").read_bytes() == observations_payload
    assert (target / "report.json").read_bytes() == report.canonical_json
    assert read_evaluation(target) == report
    assert (
        write_evaluation_bundle(
            gold_path=gold_path,
            observations_path=observations_path,
            output_root=output_root,
        )
        == target
    )

    assert (
        main(
            (
                "--gold",
                str(gold_path),
                "--observations",
                str(observations_path),
                "--output-root",
                str(output_root),
            )
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == str(target)

    (target / "report.json").write_text("{}")
    with pytest.raises(ValueError, match="already differs"):
        write_evaluation_bundle(
            gold_path=gold_path,
            observations_path=observations_path,
            output_root=output_root,
        )


@pytest.mark.parametrize(
    "relative_path",
    ("report.json", "gold.json", "observations.json"),
)
def test_read_evaluation_rejects_any_tampered_closure_file(
    tmp_path: Path, relative_path: str
) -> None:
    gold_payload = GOLD_PATH.read_bytes()
    gold = load_gold(gold_payload)
    observations_payload = json.dumps(_perfect_observations(gold), separators=(",", ":")).encode()
    gold_path = tmp_path / "gold.json"
    observations_path = tmp_path / "observations.json"
    gold_path.write_bytes(gold_payload)
    observations_path.write_bytes(observations_payload)
    target = write_evaluation_bundle(
        gold_path=gold_path,
        observations_path=observations_path,
        output_root=tmp_path / "run",
    )

    (target / relative_path).write_bytes(b"{}")

    with pytest.raises(ValueError, match=r"does not close|validation error"):
        read_evaluation(target)
