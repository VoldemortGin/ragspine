"""Independent displayed-label gold and capture evidence must agree exactly."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from tests.adapters.chart_qa_v2_fixtures import observations_fixture, targets_fixture

from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation import (
    evaluate_bar_chart_qa,
    read_bar_evaluation,
    write_bar_evaluation_bundle,
)
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import (
    BarObservations,
    BarObservedCase,
    load_bar_gold,
)

GOLD_PATH = (
    Path(__file__).parents[2] / "benchmarks/aia-2026-interim/chart-qa-bar-gold-v1.json"
)


def test_source_gold_has_two_explicit_facts_without_a_global_period() -> None:
    payload = GOLD_PATH.read_bytes()
    gold = load_bar_gold(payload)
    assert [(fact.category, fact.raw_display) for fact in gold.facts] == [
        ("1H24", "8.2%"),
        ("1H26", "6.9%"),
    ]
    assert gold.scope.global_period is None
    assert all(fact.evidence.period == fact.evidence.category for fact in gold.facts)
    assert gold.facts[1].evidence.category.bbox[2] == 571.1655999999999
    assert gold.page_context.bbox[1] > gold.figure.bbox[3]
    with pytest.raises(ValueError, match="positive"):
        load_bar_gold(payload.replace(b'"value": "8.2"', b'"value": "9.2"', 1))


def test_evaluator_accepts_exact_displayed_values_roles_context_and_release() -> None:
    payload = GOLD_PATH.read_bytes()
    gold = load_bar_gold(payload)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    report = evaluate_bar_chart_qa(
        payload,
        targets.model_dump_json().encode(),
        observations.model_dump_json().encode(),
    )
    assert report.passed
    assert report.metrics.positive_answer_coverage == "1"
    assert report.metrics.citation_exactness == "1"
    assert report.metrics.period_role_exactness == "1"
    assert report.metrics.page_context_exactness == "1"
    assert report.metrics.normalization_provenance_exactness == "1"
    assert report.metrics.hard_negative_escape_rate == "0"


def mutate_response(
    observed: BarObservedCase, path: tuple[str | int, ...], value: JsonValue
) -> BarObservedCase:
    document = TypeAdapter(dict[str, JsonValue]).validate_json(observed.response_json)
    cursor: JsonValue = document
    for key in path[:-1]:
        if isinstance(key, int):
            assert isinstance(cursor, list)
            cursor = cursor[key]
        else:
            assert isinstance(cursor, dict)
            cursor = cursor[key]
    last = path[-1]
    if isinstance(last, int):
        assert isinstance(cursor, list)
        cursor[last] = value
    else:
        assert isinstance(cursor, dict)
        cursor[last] = value
    response = json.dumps(document)
    return observed.model_copy(
        update={
            "response_json": response,
            "response_sha256": sha256(response.encode()).hexdigest(),
        }
    )


@pytest.mark.parametrize(
    ("case_id", "stratum", "denominator"),
    [
        ("wrong-series", "business_refusals", 7),
        ("cross-period-difference", "unsupported_requests", 3),
        ("missing-raw-branch", "source_and_pin_faults", 7),
    ],
)
def test_negative_strata_keep_independent_denominators_and_exact_http_outcomes(
    case_id: str, stratum: str, denominator: int
) -> None:
    payload = GOLD_PATH.read_bytes()
    gold = load_bar_gold(payload)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    positive = observations.results[0]
    changed = observations.model_copy(
        update={
            "results": tuple(
                observed.model_copy(
                    update={
                        "http_status": 200,
                        "response_json": positive.response_json,
                        "response_sha256": positive.response_sha256,
                    }
                )
                if observed.case_id == case_id
                else observed
                for observed in observations.results
            )
        }
    )
    report = evaluate_bar_chart_qa(
        payload, targets.model_dump_json().encode(), changed.model_dump_json().encode()
    )
    assert not report.passed
    strata = report.strata.model_dump()
    assert set(strata) == {
        "business_refusals",
        "unsupported_requests",
        "source_and_pin_faults",
    }
    affected = getattr(report.strata, stratum)
    assert affected.expected_cases == denominator
    assert affected.correct_cases == denominator - 1
    assert affected.unexpected_answers == 1
    assert affected.escape_rate != "0"
    assert (
        sum(
            getattr(report.strata, name).unexpected_answers
            for name in strata
            if name != stratum
        )
        == 0
    )
    assert all(
        getattr(report.strata, name).correct_response_rate == "1"
        for name in strata
        if name != stratum
    )
    case = next(case for case in report.cases if case.case_id == case_id)
    original = next(case for case in gold.cases if case.case_id == case_id)
    assert case.case_class == original.case_class
    assert case.expected_http_status == original.expected.http_status
    assert case.observed_http_status == 200
    assert case.expected_status == original.expected.business_status
    assert case.observed_status == "answered"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("answer", "value"), "-8.2"),
        (("answer", "value"), "8.3"),
        (("answer", "unit"), "bps"),
        (("answer", "raw_display"), "8.20%"),
        (("answer", "value_kind"), "estimated"),
        (("answer", "verification"), "pending"),
        (("answer", "confidence", "score"), "1"),
        (("answer", "confidence", "method"), "model guessed"),
        (("semantic_scope",), "cross_period_comparable"),
        (("snapshot_id",), "0" * 64),
        (("member_id",), "0" * 64),
        (("inputs", 0, "value"), "9.2"),
        (("inputs", 0, "period"), "1H26"),
        (("inputs", 0, "citations", 2, "raw_field_path"), "period"),
        (("inputs", 0, "citations", 2, "role"), "global_period"),
        (
            ("inputs", 0, "citations", 4, "citation", "qualification_id"),
            "qualification-v1:" + "0" * 64,
        ),
        (
            (
                "inputs",
                0,
                "citations",
                4,
                "citation",
                "occurrences",
                0,
                "anchor",
                "page_index",
            ),
            18,
        ),
        (
            (
                "inputs",
                0,
                "citations",
                4,
                "citation",
                "occurrences",
                0,
                "anchor",
                "source_revision",
            ),
            "0" * 64,
        ),
        (
            (
                "inputs",
                0,
                "citations",
                4,
                "citation",
                "occurrences",
                0,
                "anchor",
                "bbox",
                2,
            ),
            414.524,
        ),
        (
            (
                "inputs",
                0,
                "citations",
                4,
                "citation",
                "occurrences",
                0,
                "text_range",
                0,
            ),
            1,
        ),
        (("inputs", 0, "period_interpretation", "raw_field_path"), "period"),
        (("inputs", 0, "period_interpretation", "literal"), "1H26"),
        (
            ("inputs", 0, "period_interpretation", "rule_version"),
            "inferred-date-range-v1",
        ),
        (("inputs", 0, "period_interpretation", "verification"), "pending"),
        (("inputs", 0, "period_interpretation", "confidence", "score"), "0.9"),
        (("page_context", 0, "text"), "constant exchange rate basis"),
        (("page_context", 0, "source_manifest_id"), "0" * 64),
        (("page_context", 0, "scope"), "crop_evidence"),
        (("page_context", 0, "source_text_sha256"), "0" * 64),
        (("page_context", 0, "confidence", "score"), "1"),
        (("description_normalization", "rule_version"), "ignore-all-invalid-evidence"),
        (("description_normalization", "raw_response_sha256"), "0" * 64),
        (
            ("description_normalization", "receipt_id"),
            "description-normalization-v1:" + "0" * 64,
        ),
        (
            ("description_normalization", "original_typed_description_artifact_id"),
            "description-v2:" + "0" * 64,
        ),
        (
            ("description_normalization", "normalized_description_artifact_id"),
            "description-v2:" + "0" * 64,
        ),
    ],
)
def test_response_mutations_cannot_pass(
    path: tuple[str | int, ...], value: JsonValue
) -> None:
    gold_bytes = GOLD_PATH.read_bytes()
    gold = load_bar_gold(gold_bytes)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    changed = mutate_response(observations.results[0], path, value)
    observations = observations.model_copy(
        update={
            "results": (changed, *observations.results[1:]),
        }
    )
    report = evaluate_bar_chart_qa(
        gold_bytes,
        targets.model_dump_json().encode(),
        observations.model_dump_json().encode(),
    )
    assert not report.passed
    assert not report.cases[0].passed


def test_all_abstain_and_false_answer_to_missing_value_fail() -> None:
    gold_bytes = GOLD_PATH.read_bytes()
    gold = load_bar_gold(gold_bytes)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    no_answers: list[BarObservedCase] = []
    for result in observations.results:
        if result.case_id.startswith("lookup-"):
            for path, value in (
                (("status",), "abstained"),
                (("answer",), None),
                (("inputs",), []),
                (("page_context",), []),
                (("description_normalization",), None),
                (("refusal_reason",), "value_unavailable"),
            ):
                result = mutate_response(result, path, value)
        no_answers.append(result)
    abstained = BarObservations(
        schema_version=observations.schema_version,
        targets_sha256=observations.targets_sha256,
        results=tuple(no_answers),
    )
    report = evaluate_bar_chart_qa(
        gold_bytes,
        targets.model_dump_json().encode(),
        abstained.model_dump_json().encode(),
    )
    assert not report.passed
    assert report.metrics.positive_answer_coverage == "0"
    escaped = observations.results[2].model_copy(
        update={
            "response_json": observations.results[0].response_json,
            "response_sha256": observations.results[0].response_sha256,
        }
    )
    wrong = observations.model_copy(
        update={
            "results": (*observations.results[:2], escaped, *observations.results[3:])
        }
    )
    report = evaluate_bar_chart_qa(
        gold_bytes, targets.model_dump_json().encode(), wrong.model_dump_json().encode()
    )
    assert not report.passed
    assert report.metrics.hard_negative_escape_rate != "0"


def test_targets_are_independent_and_observations_are_hash_bound() -> None:
    gold_bytes = GOLD_PATH.read_bytes()
    gold = load_bar_gold(gold_bytes)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    changed_targets = (
        targets.model_dump_json()
        .encode()
        .replace(b'"snapshot_id":"' + b"2" * 64, b'"snapshot_id":"' + b"f" * 64, 1)
    )
    with pytest.raises(ValueError, match="publication"):
        evaluate_bar_chart_qa(
            gold_bytes, changed_targets, observations.model_dump_json().encode()
        )
    with pytest.raises(ValueError, match="digest"):
        evaluate_bar_chart_qa(
            gold_bytes,
            targets.model_dump_json().encode() + b"\n",
            observations.model_dump_json().encode(),
        )
    changed = observations.model_copy(update={"results": observations.results[1:]})
    assert not evaluate_bar_chart_qa(
        gold_bytes,
        targets.model_dump_json().encode(),
        changed.model_dump_json().encode(),
    ).passed


def test_four_file_bundle_keeps_targets_and_http_bytes_immutable(
    tmp_path: Path,
) -> None:
    gold_bytes = GOLD_PATH.read_bytes()
    gold = load_bar_gold(gold_bytes)
    targets = targets_fixture(gold)
    target_bytes = targets.model_dump_json().encode()
    observed_bytes = observations_fixture(gold, targets).model_dump_json().encode()
    folder = write_bar_evaluation_bundle(
        tmp_path, gold_bytes, target_bytes, observed_bytes
    )
    assert {path.name for path in folder.iterdir()} == {
        "gold.json",
        "targets.json",
        "observations.json",
        "report.json",
    }
    report = read_bar_evaluation(folder)
    assert report.passed
    assert folder.name == report.report_id
    assert folder == tmp_path / "chart-qa-v2-evaluations" / report.report_id
    assert (folder / "targets.json").read_bytes() == target_bytes
    positive_target = targets.targets[gold.cases[0].request.target].http
    assert (
        read_bar_evaluation(
            folder,
            processing_id=positive_target.processing_id,
            snapshot_id=positive_target.snapshot_id,
        )
        == report
    )
    with pytest.raises(ValueError, match="release"):
        read_bar_evaluation(
            folder, processing_id="f" * 64, snapshot_id=positive_target.snapshot_id
        )
    assert (
        write_bar_evaluation_bundle(tmp_path, gold_bytes, target_bytes, observed_bytes)
        == folder
    )
    (folder / "targets.json").write_bytes(target_bytes + b"\n")
    with pytest.raises(ValueError, match="digest"):
        read_bar_evaluation(folder)
    with pytest.raises(ValueError, match="different"):
        write_bar_evaluation_bundle(tmp_path, gold_bytes, target_bytes, observed_bytes)


def test_answering_an_expected_http_rejection_counts_as_escape() -> None:
    gold_bytes = GOLD_PATH.read_bytes()
    gold = load_bar_gold(gold_bytes)
    targets = targets_fixture(gold)
    observations = observations_fixture(gold, targets)
    first = observations.results[0]
    changed = tuple(
        observed.model_copy(
            update={
                "http_status": 200,
                "response_json": first.response_json,
                "response_sha256": first.response_sha256,
            }
        )
        if observed.case_id == "cross-period-difference"
        else observed
        for observed in observations.results
    )
    report = evaluate_bar_chart_qa(
        gold_bytes,
        targets.model_dump_json().encode(),
        observations.model_copy(update={"results": changed}).model_dump_json().encode(),
    )
    assert not report.passed
    assert report.metrics.hard_negative_escape_rate != "0"
