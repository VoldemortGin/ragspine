"""The frozen natural-language gold set parses, self-checks, and its judge is strict.

These run without the AIA store: they guard the schema, the gold file's own consistency
and the single pass/fail rule both runners share.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from enterprise_pdf_rag.adapters.http.chat import render_message
from enterprise_pdf_rag.adapters.nl_gold import (
    CITATION_BLOCK,
    NlGoldCase,
    ObservedAnswer,
    ObservedFilters,
    answer_prose,
    judge,
    load_gold,
)
from enterprise_pdf_rag.answers.models import (
    AnswerResult,
    AnswerStatus,
    ClaimCitation,
    ClaimKind,
    VerifiedClaim,
)
from enterprise_pdf_rag.processing.context_builder import BlockKind

ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS = ROOT / "data" / "benchmarks" / "enterprise-pdf-rag" / "aia-2026-interim"
GOLD_PATH = BENCHMARKS / "nl-answers-gold-v1.json"


def payload() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(GOLD_PATH.read_bytes())
    return parsed


def reload(mutated: dict[str, Any]) -> None:
    load_gold(json.dumps(mutated).encode())


def single_anchor_case(mutated: dict[str, Any]) -> dict[str, Any]:
    """The first positive case whose first requirement names one anchor, not alternatives."""
    return next(
        case
        for case in mutated["cases"]
        if case["case_class"] == "positive"
        and case["expected"].get("required_claims")
        and "any_of" not in case["expected"]["required_claims"][0]
    )


def single_filter_case(mutated: dict[str, Any]) -> dict[str, Any]:
    """The first case whose `filters_expected` names one filter set, not alternatives."""
    return next(
        case
        for case in mutated["cases"]
        if isinstance(case["expected"].get("filters_expected"), dict)
        and "any_of" not in case["expected"]["filters_expected"]
    )


# The two anchors that answer "what was the record Operating ROE": the sentence on p.3 and
# the chart point on p.7. Either is legitimate, so a requirement may name both.
ROE_QUOTE_ANCHOR: dict[str, Any] = {
    "kind": "quote",
    "page_index": 3,
    "field_path": "fragments.span-v1-ff",
    "quote": "record Operating ROE of 17.5%",
}
ROE_POINT_ANCHOR: dict[str, Any] = {
    "kind": "chart_value",
    "page_index": 7,
    "field_path": "points.point-1h26-roe-17.5.value",
    "text": "17.5%",
}


# The two pre-filter sets one Chinese question really derived on two cold runs: the union
# includes what the model's own translation derived, so its wording moves the result.
DONUT_FILTERS_1H: dict[str, Any] = {"periods": ["1H2026"], "regions": []}
DONUT_FILTERS_UNION: dict[str, Any] = {"periods": ["1H2026", "Y2026"], "regions": []}


def test_the_frozen_gold_set_loads_and_covers_every_case_class() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())

    assert gold.schema_version == "nl-answers-gold-v1"
    assert len(gold.cases) >= 20
    classes = {case.case_class for case in gold.cases}
    assert classes == {"positive", "abstain", "adversarial"}
    assert len({case.case_id for case in gold.cases}) == len(gold.cases)
    # One document, one pinned release; a case never names another corpus.
    assert {case.document_sha256 for case in gold.cases} == {gold.pinned.document_sha256}
    # Every case is runnable by at least one runner, and the offline ones script an answer.
    for case in gold.cases:
        assert case.model_output is not None or case.request.repeat_of is not None
        assert case.question.text
    # A gap is a debt, not a fixture: the rule worth pinning is that a case declaring one
    # says what it is, never that the file still owes one. The frozen set currently declares
    # no gap at all - the last two were fixed and flipped to positive cases - and this
    # passes on an empty list while still failing the day a silent gap is added.
    assert all(case.expected.known_gap_detail for case in gold.cases if case.expected.known_gap)
    adversarial = [case for case in gold.cases if case.case_class == "adversarial"]
    assert adversarial and all(case.offline_only for case in adversarial)


def test_the_benchmark_manifest_registers_this_gold_set_without_drift() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())
    registry = json.loads((BENCHMARKS / "manifest.json").read_bytes())["gold_sets"]
    entry = next(item for item in registry["sets"] if item["file"] == GOLD_PATH.name)

    assert entry["schema_version"] == gold.schema_version
    assert entry["case_count"] == len(gold.cases)
    assert entry["pinned_document_sha256"] == gold.pinned.document_sha256
    assert entry["pinned_processing_id"] == gold.pinned.processing_id
    assert entry["pinned_snapshot_id"] == gold.pinned.snapshot_id
    assert entry["pinned_member_count"] == gold.pinned.member_count
    assert entry["pinned_embedding_fingerprint"] == gold.pinned.embedding_fingerprint
    # The two chart-QA golds stay registered beside it.
    assert {item["file"] for item in registry["sets"]} == {
        "chart-qa-gold-v1.json",
        "chart-qa-bar-gold-v1.json",
        GOLD_PATH.name,
    }


def test_a_positive_case_that_expects_an_abstention_is_refused() -> None:
    mutated = payload()
    positive = next(case for case in mutated["cases"] if case["case_class"] == "positive")
    positive["expected"]["status"] = "abstained"

    with pytest.raises(ValidationError, match="must expect an answer"):
        reload(mutated)


def test_duplicate_case_ids_are_refused() -> None:
    mutated = payload()
    mutated["cases"].append(dict(mutated["cases"][0]))

    with pytest.raises(ValidationError, match="unique"):
        reload(mutated)


def test_a_positive_case_must_freeze_or_declare_its_claim() -> None:
    mutated = payload()
    positive = next(case for case in mutated["cases"] if case["case_class"] == "positive")
    positive["expected"]["required_claims"] = []

    with pytest.raises(ValidationError, match="freezes no claim"):
        reload(mutated)


def test_a_positive_case_must_demand_at_least_one_claim() -> None:
    mutated = payload()
    positive = next(case for case in mutated["cases"] if case["case_class"] == "positive")
    positive["expected"]["min_claims"] = 0

    with pytest.raises(ValidationError, match="verifies no claim"):
        reload(mutated)


def test_a_grounded_only_case_cannot_also_freeze_a_claim() -> None:
    mutated = payload()
    case = next(item for item in mutated["cases"] if item["expected"].get("grounded_only"))
    case["expected"]["required_claims"] = [
        {"kind": "quote", "page_index": 3, "field_path": "fragments.x", "quote": "x"}
    ]

    with pytest.raises(ValidationError, match="freezes no claim but still needs one"):
        reload(mutated)


def test_a_known_gap_must_say_what_the_gap_is() -> None:
    # Synthesised rather than found: the frozen set declares no gap, and the loader's rule
    # must keep being guarded whatever the file happens to contain.
    mutated = payload()
    mutated["cases"][0]["expected"] |= {"known_gap": True, "known_gap_detail": None}

    with pytest.raises(ValidationError, match="must say what the gap is"):
        reload(mutated)


def test_a_required_claim_names_exactly_one_path_form() -> None:
    mutated = payload()
    positive = single_anchor_case(mutated)
    positive["expected"]["required_claims"][0]["field_path_prefix"] = "fragments."

    with pytest.raises(ValidationError, match="field_path"):
        reload(mutated)


def test_a_requirement_may_name_alternative_anchors() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())

    # p01 accepts either evidence for the record Operating ROE: the p.3 sentence the
    # recorded run quoted, or the p.7 chart point a later run cited instead.
    (requirement,) = gold.case("p01-roe-quote-en").expected.required_claims
    assert [(item.kind, item.page_index) for item in requirement.alternatives] == [
        ("quote", 3),
        ("chart_value", 7),
    ]
    # A single anchor judges through the same interface: it is its own only alternative.
    single = next(
        item
        for case in gold.cases
        for item in case.expected.required_claims
        if len(item.alternatives) == 1
    )
    assert single.alternatives == (single,)


def test_a_requirement_with_fewer_than_two_alternatives_is_refused() -> None:
    for alternatives in ([], [ROE_QUOTE_ANCHOR]):
        mutated = payload()
        single_anchor_case(mutated)["expected"]["required_claims"][0] = {"any_of": alternatives}

        with pytest.raises(ValidationError, match="at least two"):
            reload(mutated)


def test_nested_alternatives_are_refused() -> None:
    mutated = payload()
    single_anchor_case(mutated)["expected"]["required_claims"][0] = {
        "any_of": [ROE_QUOTE_ANCHOR, {"any_of": [ROE_QUOTE_ANCHOR, ROE_POINT_ANCHOR]}]
    }

    with pytest.raises(ValidationError, match="never nested"):
        reload(mutated)


def test_an_unknown_key_beside_the_alternatives_is_refused() -> None:
    mutated = payload()
    single_anchor_case(mutated)["expected"]["required_claims"][0] = {
        "any_of": [ROE_QUOTE_ANCHOR, ROE_POINT_ANCHOR],
        "either_of": [],
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        reload(mutated)


def test_an_unknown_key_inside_an_alternative_is_refused() -> None:
    mutated = payload()
    single_anchor_case(mutated)["expected"]["required_claims"][0] = {
        "any_of": [{**ROE_QUOTE_ANCHOR, "invented": "x"}, ROE_POINT_ANCHOR]
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        reload(mutated)


def test_a_filter_expectation_may_name_alternative_filters() -> None:
    gold = load_gold(GOLD_PATH.read_bytes())

    # p06 is translated before retrieval and the pre-filters union what the translation
    # derives, so its periods follow the model's wording: two cold runs derived `1H2026`
    # alone and `1H2026` with `Y2026`, and both are true.
    expectation = gold.case("p06-donut-zh").expected.filters_expected
    assert expectation is not None
    assert [item.periods for item in expectation.alternatives] == [
        ("1H2026",),
        ("1H2026", "Y2026"),
    ]
    # A single filter set judges through the same interface: it is its own only alternative.
    single = gold.case("p01-roe-quote-en").expected.filters_expected
    assert single is not None and single.alternatives == (single,)


def test_a_filter_expectation_with_fewer_than_two_alternatives_is_refused() -> None:
    for alternatives in ([], [DONUT_FILTERS_1H]):
        mutated = payload()
        single_filter_case(mutated)["expected"]["filters_expected"] = {"any_of": alternatives}

        with pytest.raises(ValidationError, match="at least two"):
            reload(mutated)


def test_nested_filter_alternatives_are_refused() -> None:
    mutated = payload()
    single_filter_case(mutated)["expected"]["filters_expected"] = {
        "any_of": [DONUT_FILTERS_1H, {"any_of": [DONUT_FILTERS_1H, DONUT_FILTERS_UNION]}]
    }

    with pytest.raises(ValidationError, match="never nested"):
        reload(mutated)


def test_an_unknown_key_beside_the_filter_alternatives_is_refused() -> None:
    mutated = payload()
    single_filter_case(mutated)["expected"]["filters_expected"] = {
        "any_of": [DONUT_FILTERS_1H, DONUT_FILTERS_UNION],
        "either_of": [],
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        reload(mutated)


def test_an_unknown_key_inside_a_filter_alternative_is_refused() -> None:
    mutated = payload()
    single_filter_case(mutated)["expected"]["filters_expected"] = {
        "any_of": [{**DONUT_FILTERS_1H, "invented": "x"}, DONUT_FILTERS_UNION]
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        reload(mutated)


def test_an_unknown_required_citation_field_is_refused() -> None:
    mutated = payload()
    positive = next(case for case in mutated["cases"] if case["case_class"] == "positive")
    positive["expected"]["required_citation_fields"] = ["invented"]

    with pytest.raises(ValidationError, match="Unknown required citation fields"):
        reload(mutated)


def test_a_case_repeating_an_unknown_case_is_refused() -> None:
    mutated = payload()
    mutated["cases"][0]["request"] = {"repeat_of": "no-such-case"}

    with pytest.raises(ValidationError, match="repeats an unknown case"):
        reload(mutated)


def _case(expected: dict[str, object] | None = None, case_class: str = "positive") -> NlGoldCase:
    case = {
        "case_id": "unit",
        "case_class": case_class,
        "question": {"en": "q"},
        "document_sha256": "d" * 64,
        "expected": {
            "status": "answered",
            "min_claims": 1,
            "required_claims": [
                {
                    "kind": "chart_value",
                    "page_index": 17,
                    "field_path": "points.point-agency.value",
                    "quote": "72%",
                    "text": "72%",
                    "value": "72",
                    "unit": "%",
                }
            ],
            "required_citation_fields": ["member_id", "field_path", "quote"],
            **(expected or {}),
        },
        "model_output": {"answer": "72%.", "claims": []},
        "rationale": "unit",
    }
    return NlGoldCase.model_validate_json(json.dumps(case))


def _observed(overrides: dict[str, object] | None = None) -> ObservedAnswer:
    envelope: dict[str, object] = {
        "status": "answered",
        "abstain_reason": None,
        "abstain_detail": None,
        "claims": [
            {
                "claim_id": "whatever",
                "kind": "chart_value",
                "text": "72%",
                "value": "72",
                "unit": "%",
                "citations": [
                    {
                        "member_id": "m" * 64,
                        "kind": "chart",
                        "page_index": 17,
                        "field_path": "points.point-agency.value",
                        "evidence_ids": ["obs-867ffed1c4f36f65"],
                        "quote": "72%",
                    },
                    {
                        "member_id": "m" * 64,
                        "kind": "chart",
                        "page_index": 17,
                        "field_path": "points.point-agency.series",
                        "evidence_ids": ["obs-714a8afa9b25a4e8"],
                        "quote": "VONB",
                    },
                ],
            }
        ],
        "cache_hit": False,
        "filters_relaxed": False,
    }
    envelope.update(overrides or {})
    return ObservedAnswer.model_validate(envelope)


def test_a_matching_answer_passes_and_ignores_the_unasserted_citations() -> None:
    assert judge(_case(), _observed(), "Agency contributed 72% of VONB.") == ()


def test_a_changed_chart_value_fails_the_case() -> None:
    observed = _observed()
    wrong = observed.model_copy(
        update={"claims": (observed.claims[0].model_copy(update={"value": "75"}),)}
    )

    (failure,) = judge(_case(), wrong, "75%.")
    assert "value is '75'" in failure


def test_a_claim_on_another_page_does_not_satisfy_the_requirement() -> None:
    observed = _observed()
    citations = tuple(
        citation.model_copy(update={"page_index": 13}) for citation in observed.claims[0].citations
    )
    moved = observed.model_copy(
        update={"claims": (observed.claims[0].model_copy(update={"citations": citations}),)}
    )

    (failure,) = judge(_case(), moved, "72%.")
    assert failure.startswith("missing required claim")


def test_any_one_alternative_satisfies_a_requirement() -> None:
    case = _case(
        {
            "required_claims": [
                {
                    "any_of": [
                        ROE_QUOTE_ANCHOR,
                        {
                            "kind": "chart_value",
                            "page_index": 17,
                            "field_path": "points.point-agency.value",
                            "quote": "72%",
                            "text": "72%",
                            "value": "72",
                            "unit": "%",
                        },
                    ]
                }
            ]
        }
    )

    # Only the second alternative is cited, and that is enough.
    assert judge(case, _observed(), "Agency contributed 72% of VONB.") == ()


def test_a_requirement_no_alternative_matches_fails_and_names_them_all() -> None:
    case = _case(
        {
            "required_claims": [
                {
                    "any_of": [
                        ROE_QUOTE_ANCHOR,
                        {
                            "kind": "chart_value",
                            "page_index": 17,
                            "field_path": "points.point-partnerships.value",
                            "text": "28%",
                        },
                    ]
                }
            ]
        }
    )

    (failure,) = judge(case, _observed(), "72%.")
    assert "no alternative" in failure
    assert "fragments.span-v1-ff" in failure
    assert "points.point-partnerships.value" in failure


def test_an_alternative_that_matches_but_misreads_the_value_still_fails() -> None:
    case = _case(
        {
            "required_claims": [
                {
                    "any_of": [
                        ROE_QUOTE_ANCHOR,
                        {
                            "kind": "chart_value",
                            "page_index": 17,
                            "field_path": "points.point-agency.value",
                            "value": "75",
                        },
                    ]
                }
            ]
        }
    )

    (failure,) = judge(case, _observed(), "72%.")
    assert "no alternative" in failure
    assert "value is '72', expected '75'" in failure


def test_a_matching_citation_that_lacks_a_required_field_fails_the_case() -> None:
    observed = _observed()
    citations = (observed.claims[0].citations[0].model_copy(update={"member_id": None}),)
    blank = observed.model_copy(
        update={"claims": (observed.claims[0].model_copy(update={"citations": citations}),)}
    )

    failures = judge(_case(), blank, "72%.")
    assert any("citation lacks member_id" in failure for failure in failures)


def test_a_forbidden_number_in_the_prose_fails_an_answered_case() -> None:
    case = _case({"forbidden_numbers": ["44"]})

    (failure,) = judge(case, _observed(), "Agency is 72%, 44 points above Partnerships.")
    assert "forbidden number '44'" in failure


def test_an_abstention_has_no_prose_to_police() -> None:
    case = _case(
        {
            "status": "abstained",
            "min_claims": 0,
            "required_claims": [],
            "abstain_reason": "model_declined",
            "forbidden_numbers": ["44"],
        },
        case_class="abstain",
    )
    observed = _observed({"status": "abstained", "abstain_reason": "model_declined", "claims": []})

    assert judge(case, observed, "无法基于已验证证据回答 (model_declined): 44") == ()


def test_filters_and_relaxation_are_compared_exactly() -> None:
    case = _case(
        {"filters_expected": {"periods": ["1H2026"], "regions": []}, "filters_relaxed": False}
    )

    assert (
        judge(case, _observed({"filters_applied": {"periods": ["1H2026"], "regions": []}}), "")
        == ()
    )
    failures = judge(case, _observed({"filters_applied": None, "filters_relaxed": True}), "")
    assert len(failures) == 2


def test_an_absent_filter_expectation_is_written_as_an_empty_object() -> None:
    case = _case({"filters_expected": {"periods": [], "regions": []}})

    assert judge(case, _observed({"filters_applied": None}), "") == ()
    assert judge(case, _observed({"filters_applied": {"periods": ["Y2024"], "regions": []}}), "")


def test_any_one_alternative_filter_set_satisfies_the_expectation() -> None:
    case = _case({"filters_expected": {"any_of": [DONUT_FILTERS_1H, DONUT_FILTERS_UNION]}})

    for applied in (DONUT_FILTERS_1H, DONUT_FILTERS_UNION):
        assert judge(case, _observed({"filters_applied": applied}), "72%.") == ()


def test_filters_matching_no_alternative_fail_and_name_every_one() -> None:
    case = _case({"filters_expected": {"any_of": [DONUT_FILTERS_1H, DONUT_FILTERS_UNION]}})

    applied = {"periods": ["Y2026"], "regions": []}
    (failure,) = judge(case, _observed({"filters_applied": applied}), "72%.")
    assert "no alternative" in failure
    assert "filters_applied is {'periods': ('Y2026',), 'regions': ()}" in failure
    assert "{'periods': ('1H2026',), 'regions': ()}" in failure
    assert "{'periods': ('1H2026', 'Y2026'), 'regions': ()}" in failure


def test_a_grounded_only_case_only_demands_a_fully_cited_claim() -> None:
    case = _case(
        {
            "min_claims": 1,
            "required_claims": [],
            "grounded_only": True,
            "required_citation_fields": ["member_id", "page_title"],
        }
    )

    (failure,) = judge(case, _observed(), "72%.")
    assert "citation lacks page_title" in failure
    titled = _observed()
    citation = titled.claims[0].citations[0].model_copy(update={"page_title": "Some page"})
    with_title = titled.model_copy(
        update={"claims": (titled.claims[0].model_copy(update={"citations": (citation,)}),)}
    )
    assert judge(case, with_title, "72%.") == ()


def test_the_offline_runner_may_skip_the_cache_expectation() -> None:
    case = _case({"cache_hit": True})

    assert judge(case, _observed(), "") != ()
    assert judge(case, _observed(), "", skip=frozenset({"cache_hit"})) == ()


def test_the_prose_split_matches_the_rendered_assistant_message() -> None:
    claim = VerifiedClaim(
        "c1",
        ClaimKind.QUOTE,
        "record Operating ROE of 17.5%",
        None,
        None,
        (
            ClaimCitation(
                "m" * 64,
                BlockKind.TEXT,
                3,
                "fragments.span-v1-ff",
                ("span-v1-ff",),
                None,
                "record Operating ROE of 17.5% ",
            ),
        ),
    )
    result = AnswerResult(
        AnswerStatus.ANSWERED,
        "AIA's record Operating ROE in 1H 2026 was 17.5%.",
        (claim,),
        (),
        None,
        None,
        "d" * 64,
        "p" * 64,
        "s" * 64,
        ("m" * 64,),
        (),
        None,
        1,
        False,
    )
    message = render_message(result)

    assert CITATION_BLOCK in message
    assert answer_prose(message) == result.answer


def test_an_observed_answer_parses_a_real_rag_chat_envelope_and_ignores_new_fields() -> None:
    observed = ObservedAnswer.model_validate(
        {
            "schema_version": "rag-chat-v1",
            "status": "answered",
            "abstain_reason": None,
            "abstain_detail": None,
            "document_sha256": "d" * 64,
            "processing_id": "p" * 64,
            "snapshot_id": "s" * 64,
            "member_ids": ["m" * 64],
            "claims": [],
            "rejected": [],
            "llm_live_calls": 1,
            "cache_hit": True,
            "member_ranks": [{"member_id": "m" * 64, "fused_score": 0.03}],
            "filters_applied": {"periods": ["1H2026"], "regions": []},
            "filters_relaxed": False,
        }
    )

    assert observed.cache_hit is True
    assert observed.filters_applied == ObservedFilters(periods=("1H2026",))
