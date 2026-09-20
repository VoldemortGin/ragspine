"""The answer service retrieves, calls the model exactly once, verifies and abstains."""

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Never

import pytest

from enterprise_pdf_rag.adapters.answer_service import (
    AmbiguousDocument,
    AnswerService,
    AnswerSettings,
    DependencyUnavailable,
    UnknownDocument,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.hybrid_search import FusedHit
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    ClaimKind,
    MemberFilters,
)
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, ModelClaim
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import BlockKind
from enterprise_pdf_rag.processing.models import ObjectKind
from enterprise_pdf_rag.processing.typed_ir import DiagramIR, DiagramNode
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    DOCUMENT_LABEL,
    publish_generic_document,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import MULTI_HEADER_TABLE
from tests.enterprise_pdf_rag.answers.fake_document import (
    DIAGRAM_ANCHOR,
    DIAGRAM_LABELS,
    DONUT_TITLE,
    FakeDocument,
    FakeMember,
    diagram_ir,
    donut_chart,
    formula_ir,
    pending_chart,
)
from tests.enterprise_pdf_rag.answers.fake_llm import (
    Script,
    answered,
    chart_claim,
    declined,
    scripted_client,
)
from tests.enterprise_pdf_rag.answers.store_mounted_document import (
    StoreMountedDocument,
    bar_document,
)

_MEMBER_LINE = re.compile(r"^\[member ([0-9a-f]{64})\] kind=(\w+)", re.MULTILINE)
_QUESTION = "What was the expense ratio in 1H21?"


def _members(prompt: str, kind: str) -> list[str]:
    return [member for member, found in _MEMBER_LINE.findall(prompt) if found == kind]


@dataclass
class _CountingJudge:
    calls: int = 0

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls += 1
        return list(range(len(candidates)))


class _ExplodingJudge:
    def judge(self, query: str, candidates: list[str]) -> Never:
        raise AssertionError("rerank is off by default; the judge must not be consulted")


@pytest.fixture
def bar(tmp_path: Path) -> tuple[StoreMountedDocument, str]:
    document, pin = bar_document(tmp_path)
    return document, pin.member_id


def _service(
    tmp_path: Path,
    document: StoreMountedDocument | FakeDocument,
    script: Script,
    *,
    settings: AnswerSettings | None = None,
    reranker: _CountingJudge | _ExplodingJudge | None = None,
) -> tuple[AnswerService, list[str]]:
    client, prompts = scripted_client(tmp_path / "llm", script)
    service = AnswerService(
        {document.source_sha256: document}, client, settings=settings, reranker=reranker
    )
    return service, prompts


def _one_chart_claim(text: str, point: str = "p-1H21", answer: str | None = None) -> Script:
    def script(prompt: str) -> ModelAnswer:
        (chart,) = _members(prompt, "chart")
        return answered(
            answer or f"The expense ratio in {point[2:]} was {text}.",
            chart_claim(chart, point, text),
        )

    return script


def test_correct_chart_citation_answers_with_exactly_one_model_call_and_full_provenance(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, chart_member = bar
    service, prompts = _service(tmp_path, document, _one_chart_claim("15%"))
    request = AnswerRequest(_QUESTION)

    result = service.answer(request)

    assert isinstance(result, AnswerResult)
    assert result.status is AnswerStatus.ANSWERED
    assert result.answer == "The expense ratio in 1H21 was 15%."
    assert (result.abstain_reason, result.abstain_detail) == (None, None)
    assert len(prompts) == 1 and result.llm_live_calls == 1 and result.cache_hit is False
    assert result.request_fingerprint is not None
    # Provenance: the pinned identities and the exact field-level citation survive.
    assert result.document_sha256 == document.source_sha256
    assert result.processing_id == document.processing_id
    assert result.snapshot_id == document.retrieval_snapshot_id
    assert chart_member in result.member_ids
    assert all(
        isinstance(hit, FusedHit) and hit.snapshot_id == result.snapshot_id for hit in result.fused
    )
    assert [hit.member_id for hit in result.fused][: len(result.member_ids)] == list(
        result.member_ids
    )
    (claim,) = result.claims
    assert result.rejected == ()
    assert (claim.kind, claim.text, claim.value, claim.unit) == (
        ClaimKind.CHART_VALUE,
        "15%",
        Decimal("15"),
        "%",
    )
    citation = claim.citations[0]
    assert (citation.member_id, citation.kind, citation.page_index) == (
        chart_member,
        BlockKind.CHART,
        0,
    )
    assert citation.field_path == "points.p-1H21.value"
    assert citation.chart_citation is not None
    assert citation.chart_citation.field_path == "points.p-1H21.value"
    assert citation.chart_citation.occurrences and citation.evidence_ids
    # The prompt carried the rules and the citable paths, and nothing was truncated.
    assert "points.p-1H21.value: series=Expense Ratio category=1H21 unit=% value=15" in prompts[0]
    # The displayed projection carries explicit points only; 1H22 is not offered.
    assert "points.p-1H23.value" in prompts[0] and "p-1H22" not in prompts[0]
    assert _QUESTION in prompts[0]
    assert "instructions" in SYSTEM_RULES

    # Same question, same snapshot: replayed from the immutable cache, zero live calls.
    again = service.answer(request)
    assert again.cache_hit is True and again.llm_live_calls == 0 and len(prompts) == 1
    assert (again.status, again.answer, again.claims) == (
        result.status,
        result.answer,
        result.claims,
    )


def test_wrong_value_is_dropped_while_the_verified_claim_answers(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar

    def script(prompt: str) -> ModelAnswer:
        (chart,) = _members(prompt, "chart")
        return answered(
            "In 1H21 the expense ratio was 15%.",
            chart_claim(chart, "p-1H21", "15%", claim_id="good"),
            chart_claim(chart, "p-1H23", "7%", claim_id="bad"),
        )

    service, _ = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest(_QUESTION))
    assert result.status is AnswerStatus.ANSWERED
    assert [claim.claim_id for claim in result.claims] == ["good"]
    (rejected,) = result.rejected
    assert (rejected.claim_id, rejected.reason) == ("bad", AbstainReason.CLAIM_NOT_IN_EVIDENCE)
    assert rejected.field_path == "points.p-1H23.value" and rejected.text == "7%"


def test_unavailable_period_abstains_with_the_chart_reason(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, prompts = _service(tmp_path, document, _one_chart_claim("10%", point="p-1H22"))
    result = service.answer(AnswerRequest("What was the expense ratio in 1H22?"))
    assert len(prompts) == 1
    assert result.status is AnswerStatus.ABSTAINED and result.answer is None
    assert result.abstain_reason is AbstainReason.VALUE_UNAVAILABLE
    assert result.claims == () and [r.reason for r in result.rejected] == [
        AbstainReason.VALUE_UNAVAILABLE
    ]


def test_prose_number_outside_verified_claims_abstains_as_a_whole(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar

    def script(prompt: str) -> ModelAnswer:
        (chart,) = _members(prompt, "chart")
        return answered(
            "It fell 9 percentage points, from 15% in 1H21 to 6% in 1H23.",
            chart_claim(chart, "p-1H21", "15%", claim_id="a"),
            chart_claim(chart, "p-1H23", "6%", claim_id="b"),
        )

    service, _ = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest("How did the expense ratio change from 1H21 to 1H23?"))
    assert result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert result.abstain_detail is not None and "9" in result.abstain_detail
    assert [claim.claim_id for claim in result.claims] == ["a", "b"]  # kept for audit
    assert result.answer is None


def test_prose_may_repeat_numbers_from_the_question_but_not_invent_new_ones(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    question = "What was the expense ratio in 1H21, the first half of 2021?"
    service, _ = _service(
        tmp_path, document, _one_chart_claim("15%", answer="In 1H21 (2021) it was 15%.")
    )
    result = service.answer(AnswerRequest(question))
    assert (result.status, result.answer) == (AnswerStatus.ANSWERED, "In 1H21 (2021) it was 15%.")

    invented, _ = _service(
        tmp_path / "invented",
        document,
        _one_chart_claim("15%", answer="In 1H21 (2021) it was 15%, up from 12%."),
    )
    result = invented.answer(AnswerRequest(question))
    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
    )
    assert result.abstain_detail is not None and "12%" in result.abstain_detail
    assert "2021" not in result.abstain_detail


def test_prose_may_repeat_numbers_from_the_cited_evidence_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-2025.pdf",
        label="Revenue 2025",
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
    )
    document = StoreMountedDocument(
        LocalDocumentStore(Path(publication.source_store), activate_on_publish=False),
        ProcessingStore(Path(publication.processing_store)),
        processing_id=publication.current_processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    fragment = re.compile(r"^fragments\.(\S+): (.*page 2.*)$", re.MULTILINE)

    def script(prompt: str) -> ModelAnswer:
        blocks = prompt.split("[member ")[1:]
        block = next(block for block in blocks if "page 2" in block)
        found = fragment.search(block)
        assert found is not None
        span_id, text = found.groups()
        assert text == "Revenue 2025 page 2"
        return answered(
            "Page 2 covers 2025 revenue.",
            ModelClaim(
                claim_id="q",
                member_id=block[:64],
                kind="quote",
                field_path=f"fragments.{span_id}",
                text="revenue",
            ),
        )

    service, _ = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest("What does the second page cover?", top_k=3))
    assert result.status is AnswerStatus.ANSWERED, result
    (claim,) = result.claims
    assert claim.text == "revenue" and "2025" in claim.citations[0].quote


def test_zero_verified_claims_and_model_declines_abstain(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, _ = _service(tmp_path, document, lambda prompt: answered("It was about 12%."))
    result = service.answer(AnswerRequest(_QUESTION))
    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.NO_VERIFIED_CLAIM,
    )
    service, _ = _service(tmp_path / "second", document, lambda prompt: declined("ambiguous"))
    result = service.answer(AnswerRequest(_QUESTION))
    assert (result.status, result.abstain_reason, result.abstain_detail) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_DECLINED,
        "ambiguous",
    )


def test_rerank_is_off_unless_requested_and_needs_an_injected_judge(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    exploding, _ = _service(tmp_path, document, _one_chart_claim("15%"), reranker=_ExplodingJudge())
    assert exploding.answer(AnswerRequest(_QUESTION)).status is AnswerStatus.ANSWERED

    judge = _CountingJudge()
    counting, _ = _service(tmp_path / "on", document, _one_chart_claim("15%"), reranker=judge)
    assert counting.answer(AnswerRequest(_QUESTION, rerank=True)).status is AnswerStatus.ANSWERED
    assert judge.calls == 1

    plain, prompts = _service(tmp_path / "none", document, _one_chart_claim("15%"))
    with pytest.raises(DependencyUnavailable, match="rerank"):
        plain.answer(AnswerRequest(_QUESTION, rerank=True))
    assert prompts == []


def test_budget_without_a_surviving_block_abstains_before_any_model_call(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, prompts = _service(
        tmp_path, document, _one_chart_claim("15%"), settings=AnswerSettings(prompt_budget_chars=1)
    )
    result = service.answer(AnswerRequest(_QUESTION))
    assert prompts == [] and result.llm_live_calls == 0
    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.NO_RELEVANT_MEMBER,
    )
    assert result.member_ids == () and result.fused != ()


def test_malformed_model_json_abstains_and_provider_failures_are_dependency_errors(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, _ = _service(tmp_path, document, lambda prompt: "not json at all")
    result = service.answer(AnswerRequest(_QUESTION))
    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_OUTPUT_INVALID,
    )
    assert result.llm_live_calls == 1
    client, prompts = scripted_client(
        tmp_path / "exhausted", _one_chart_claim("15%"), max_live_calls=0
    )
    exhausted = AnswerService({document.source_sha256: document}, client)
    with pytest.raises(DependencyUnavailable, match="call_budget_exhausted"):
        exhausted.answer(AnswerRequest(_QUESTION))
    assert prompts == []


def test_document_selection_requires_an_unambiguous_target(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    client, _ = scripted_client(tmp_path / "llm", _one_chart_claim("15%"))
    single = AnswerService({document.source_sha256: document}, client)
    assert (
        single.answer(AnswerRequest(_QUESTION, document_sha256=None)).status
        is AnswerStatus.ANSWERED
    )
    with pytest.raises(UnknownDocument):
        single.answer(AnswerRequest(_QUESTION, document_sha256="0" * 64))
    two = AnswerService({document.source_sha256: document, "0" * 64: document}, client)
    with pytest.raises(AmbiguousDocument):
        two.answer(AnswerRequest(_QUESTION))
    with pytest.raises(ValueError, match="question"):
        AnswerRequest("   ")
    with pytest.raises(ValueError, match="top_k"):
        AnswerRequest(_QUESTION, top_k=0)


def test_text_quotes_are_verified_verbatim_against_the_published_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Revenue expense ratio",
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
    )
    document = StoreMountedDocument(
        LocalDocumentStore(Path(publication.source_store), activate_on_publish=False),
        ProcessingStore(Path(publication.processing_store)),
        processing_id=publication.current_processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert document.source_sha256 == publication.source_sha256
    fragment = re.compile(r"^fragments\.(\S+): (.*page 2.*)$", re.MULTILINE)

    def quote(prompt: str) -> ModelAnswer:
        blocks = prompt.split("[member ")[1:]
        block = next(block for block in blocks if "page 2" in block)
        member_id = block[:64]
        found = fragment.search(block)
        assert found is not None
        span_id, text = found.groups()
        return answered(
            f"Page 2 reads: {text}",
            ModelClaim(
                claim_id="q",
                member_id=member_id,
                kind="quote",
                field_path=f"fragments.{span_id}",
                text=text,
            ),
        )

    service, prompts = _service(tmp_path, document, quote)
    result = service.answer(AnswerRequest("What does page 2 say?", top_k=3))
    assert result.status is AnswerStatus.ANSWERED, result
    (claim,) = result.claims
    assert claim.kind is ClaimKind.QUOTE and "page 2" in claim.text
    (citation,) = claim.citations
    assert citation.kind is BlockKind.TEXT and citation.page_index == 1
    assert citation.evidence_ids == (citation.field_path.removeprefix("fragments."),)
    assert citation.quote == claim.text and citation.bbox is not None
    assert len(document.member_ids_by_kind(ObjectKind.TEXT)) == 3
    assert len(prompts) == 1

    def fabricate(prompt: str) -> ModelAnswer:
        blocks = prompt.split("[member ")[1:]
        block = next(block for block in blocks if "page 2" in block)
        found = fragment.search(block)
        assert found is not None
        span_id = found.group(1)
        return answered(
            "Page 2 says revenue doubled.",
            ModelClaim(
                claim_id="f",
                member_id=block[:64],
                kind="quote",
                field_path=f"fragments.{span_id}",
                text="revenue doubled",
            ),
        )

    fabricated, _ = _service(tmp_path / "fab", document, fabricate)
    result = fabricated.answer(AnswerRequest("What does page 2 say?", top_k=3))
    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
    )
    assert result.rejected[0].claim_id == "f"


def test_request_defaults_widen_the_channel_window_and_the_prompt_seats() -> None:
    request = AnswerRequest(_QUESTION)
    assert (request.top_k, request.channel_limit, request.rerank) == (10, 50, False)
    with pytest.raises(ValueError, match="channel_limit"):
        AnswerRequest(_QUESTION, channel_limit=0)


_TEXT_IDS = tuple(f"text-{index}" for index in range(1, 7))


def _label_less_diagram() -> DiagramIR:
    """A diagram whose only node carries no label: nothing an answer could cite."""
    node = DiagramNode("n1", "", (20.0, 70.0, 90.0, 100.0), ())
    return DiagramIR("diagram-blank", DIAGRAM_ANCHOR, (node,), (), (), Verification.PENDING)


def _seat_document(vector_order: tuple[str, ...]) -> FakeDocument:
    members = (
        *(FakeMember(member_id, f"Narrative sentence {member_id}") for member_id in _TEXT_IDS),
        FakeMember("donut", DONUT_TITLE, donut_chart(("Agency", "72"), ("Partnerships", "28"))),
        FakeMember("pending", "High-Quality In-Force Portfolio", pending_chart()),
        FakeMember("diagram", "Agency technology investment", visual=diagram_ir()),
        FakeMember("diagram-2", "A second proven diagram", visual=diagram_ir()),
        FakeMember("blank-diagram", "A diagram without a label", visual=_label_less_diagram()),
        FakeMember("formula", "Return on equity", visual=formula_ir()),
    )
    return FakeDocument(members, vector_order)


def _seat_result(tmp_path: Path, document: FakeDocument) -> tuple[AnswerResult, list[str]]:
    # A question no index text contains: the fused order is exactly the vector order.
    service, prompts = _service(tmp_path, document, lambda prompt: declined())
    return service.answer(AnswerRequest("zzz-nothing-matches", top_k=2)), prompts


def test_a_citable_chart_within_two_top_k_takes_the_last_prompt_seat(tmp_path: Path) -> None:
    document = _seat_document(("text-1", "text-2", "text-3", "donut", "text-4"))
    result, prompts = _seat_result(tmp_path, document)
    assert result.member_ids == ("text-1", "donut")
    assert [hit.member_id for hit in result.fused] == ["text-1", "donut"]
    # Only the chart candidates in the window were resolved for the check.
    assert document.resolved == ["text-1", "text-2", "donut"]
    assert "points.point-agency.value: series=VONB category=Agency unit=% value=72" in prompts[0]
    assert "text-2-span" not in prompts[0]


def test_no_seat_is_given_beyond_two_top_k_or_to_a_pending_chart(tmp_path: Path) -> None:
    beyond = _seat_document(("text-1", "text-2", "text-3", "text-4", "donut"))
    result, prompts = _seat_result(tmp_path, beyond)
    assert result.member_ids == ("text-1", "text-2")
    assert "donut" not in beyond.resolved and "kind=chart" not in prompts[0]

    pending = _seat_document(("text-1", "text-2", "pending", "text-3"))
    result, prompts = _seat_result(tmp_path / "pending", pending)
    assert result.member_ids == ("text-1", "text-2")
    assert pending.resolved == ["text-1", "text-2", "pending"]  # checked, not promoted
    assert "kind=chart" not in prompts[0]


def test_a_chart_already_in_the_top_k_needs_no_seat_and_no_extra_reads(tmp_path: Path) -> None:
    document = _seat_document(("donut", "text-1", "text-2", "pending"))
    result, _ = _seat_result(tmp_path, document)
    assert result.member_ids == ("donut", "text-1")
    assert document.resolved == ["donut", "text-1"]
    assert document.member_texts_calls == 1  # the lexical index build only


def test_a_proven_diagram_and_a_formula_within_two_top_k_each_take_a_seat(
    tmp_path: Path,
) -> None:
    # One seat per visual kind, given up from the last seat backward (ADR 0012 generalised).
    document = _seat_document(("text-1", "text-2", "text-3", "donut", "diagram", "text-4"))
    service, prompts = _service(tmp_path, document, lambda prompt: declined())
    result = service.answer(AnswerRequest("zzz-nothing-matches", top_k=3))
    assert result.member_ids == ("text-1", "diagram", "donut")
    assert document.resolved == ["text-1", "text-2", "text-3", "donut", "diagram"]
    assert f"nodes.n1.label: {DIAGRAM_LABELS[0]}" in prompts[0]
    assert f"nodes.n2.label: {DIAGRAM_LABELS[1]}" in prompts[0]
    assert "points.point-agency.value" in prompts[0]

    formula = _seat_document(("text-1", "text-2", "formula", "text-3"))
    service, prompts = _service(tmp_path / "formula", formula, lambda prompt: declined())
    result = service.answer(AnswerRequest("zzz-nothing-matches", top_k=2))
    assert result.member_ids == ("text-1", "formula")
    assert formula.resolved == ["text-1", "text-2", "formula"]
    assert "formula.linear: " in prompts[0]


def test_visual_seats_never_evict_a_seated_visual_and_stop_at_one_per_kind(
    tmp_path: Path,
) -> None:
    # The chart in the head keeps its seat; the diagram replaces the last non-visual one.
    document = _seat_document(("text-1", "donut", "diagram", "text-2"))
    result, _ = _seat_result(tmp_path, document)
    assert result.member_ids == ("diagram", "donut")

    # A diagram already seated closes its kind: the second one in the window is not read.
    capped = _seat_document(("diagram", "text-1", "diagram-2", "text-2"))
    result, _ = _seat_result(tmp_path / "capped", capped)
    assert result.member_ids == ("diagram", "text-1")
    assert capped.resolved == ["diagram", "text-1"]
    assert capped.member_texts_calls == 1


def test_no_visual_seat_beyond_two_top_k_or_for_a_diagram_without_a_citable_label(
    tmp_path: Path,
) -> None:
    beyond = _seat_document(("text-1", "text-2", "text-3", "text-4", "diagram", "formula"))
    result, prompts = _seat_result(tmp_path, beyond)
    assert result.member_ids == ("text-1", "text-2")
    assert "diagram" not in beyond.resolved and "formula" not in beyond.resolved
    assert "kind=diagram" not in prompts[0] and "kind=formula" not in prompts[0]

    blank = _seat_document(("text-1", "text-2", "blank-diagram", "text-3"))
    result, prompts = _seat_result(tmp_path / "blank", blank)
    assert result.member_ids == ("text-1", "text-2")
    assert blank.resolved == ["text-1", "text-2", "blank-diagram"]  # checked, not promoted
    assert "kind=diagram" not in prompts[0]


def _metadata_document(vector_order: tuple[str, ...]) -> FakeDocument:
    members = (
        FakeMember(
            "cover",
            "ACME 2026 Interim Results",
            page_title="ACME 2026 Interim Results",
            page_type="cover",
            periods=("Y2026",),
        ),
        FakeMember(
            "hk",
            "Hong Kong VONB grew strongly",
            page_title="Hong Kong",
            page_type="text",
            periods=("1H2026",),
            regions=("Hong Kong",),
        ),
        FakeMember(
            "th",
            "Thailand VONB margin expanded",
            page_title="Thailand",
            page_type="text",
            periods=("1H2026",),
            regions=("Thailand",),
        ),
        FakeMember(
            "fy",
            "Full year VONB summary",
            page_title="Group overview",
            page_type="text",
            periods=("FY2024",),
            regions=("Group",),
        ),
        FakeMember("bare", "Untagged VONB remarks"),
    )
    return FakeDocument(members, vector_order)


def test_question_periods_and_regions_narrow_the_candidates_before_ranking(
    tmp_path: Path,
) -> None:
    document = _metadata_document(("bare", "cover", "hk", "th", "fy"))
    service, prompts = _service(tmp_path / "a", document, lambda prompt: declined())
    result = service.answer(AnswerRequest("Thailand VONB in 1H26?", top_k=1))
    assert result.filters_applied == MemberFilters(("1H2026",), ("Thailand",))
    assert result.filters_relaxed is False
    assert result.member_ids == ("th",)
    assert "Hong Kong VONB" not in prompts[0] and "Untagged" not in prompts[0]

    service, _ = _service(tmp_path / "b", document, lambda prompt: declined())
    year = service.answer(AnswerRequest("VONB in 2026", top_k=2))
    assert year.filters_applied == MemberFilters(("Y2026",), ())
    assert year.filters_relaxed is False
    assert set(year.member_ids) == {"hk", "th"}  # the cover page never enters the candidates


def test_starved_filters_relax_to_the_whole_corpus_and_say_so(tmp_path: Path) -> None:
    document = _metadata_document(("bare", "cover", "hk", "th", "fy"))
    service, _ = _service(tmp_path / "a", document, lambda prompt: declined())
    result = service.answer(
        AnswerRequest("VONB overview", top_k=3, filters=MemberFilters(regions=("Mars",)))
    )
    assert result.filters_applied == MemberFilters((), ("Mars",))
    assert result.filters_relaxed is True
    assert len(result.member_ids) == 3 and "cover" not in result.member_ids

    service, _ = _service(tmp_path / "b", document, lambda prompt: declined())
    explicit = service.answer(AnswerRequest("Hong Kong 2026", top_k=1, filters=MemberFilters()))
    assert explicit.filters_applied is None and explicit.filters_relaxed is False
    assert explicit.member_ids == ("hk",)  # lexical hit, not the filter: no auto-derivation

    service, _ = _service(tmp_path / "c", document, lambda prompt: declined())
    unknown = service.answer(AnswerRequest("Singapore VONB", top_k=1))
    assert unknown.filters_applied is None  # not in this document's vocabulary


def test_claim_citations_carry_the_verified_page_title(tmp_path: Path) -> None:
    document = _metadata_document(("th", "hk"))

    def quote(prompt: str) -> ModelAnswer:
        return answered(
            "Thailand VONB margin expanded",
            ModelClaim(
                claim_id="q",
                member_id="th",
                kind="quote",
                field_path="fragments.th-span",
                text="Thailand VONB margin expanded",
            ),
        )

    service, _ = _service(tmp_path, document, quote)
    result = service.answer(AnswerRequest("Thailand margin", top_k=1))
    assert result.status is AnswerStatus.ANSWERED
    assert result.claims[0].citations[0].page_title == "Thailand"


_CELL_LINE = re.compile(
    r"^cells\.(\S+) \((\d+),(\d+)\): 1,234 row=(\d+) col=(\d+) header=(.*)$", re.MULTILINE
)


def _cell_relation_script(header: str) -> Script:
    def script(prompt: str) -> ModelAnswer:
        match = _CELL_LINE.search(prompt)
        assert match is not None
        (table,) = _members(prompt, "table")
        return answered(
            "Revenue under Value is 1,234.",
            ModelClaim(
                claim_id="c1",
                member_id=table,
                kind="cell",
                field_path=f"cells.{match[1]}",
                text="1,234",
                row=int(match[4]),
                col=int(match[5]),
                header=header,
            ),
        )

    return script


def _ruled_table_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreMountedDocument:
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-semiannual.pdf",
        label=DOCUMENT_LABEL,
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
        table_page=MULTI_HEADER_TABLE,
    )
    return StoreMountedDocument(
        LocalDocumentStore(Path(published.source_store), activate_on_publish=False),
        ProcessingStore(Path(published.processing_store)),
        processing_id=published.published_processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )


def test_table_cell_claim_with_proved_header_answers_and_cites_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _ruled_table_document(tmp_path, monkeypatch)
    service, prompts = _service(tmp_path, document, _cell_relation_script("Value"))
    result = service.answer(
        AnswerRequest("What is the value in the second row, first column under Value?")
    )
    assert result.status is AnswerStatus.ANSWERED, result
    (claim,) = result.claims
    (citation,) = claim.citations
    assert (citation.row, citation.col, citation.header) == (2, 1, "Value")
    assert citation.header_cell_id in citation.evidence_ids
    assert 'header="Group" | "Value"' in prompts[0]


def test_table_cell_claim_with_unproved_header_text_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _ruled_table_document(tmp_path, monkeypatch)
    service, _ = _service(tmp_path, document, _cell_relation_script("value"))
    result = service.answer(
        AnswerRequest("What is the value in the second row, first column under Value?")
    )
    assert result.status is AnswerStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert result.rejected[0].detail.startswith("claimed header")
