"""The answer service retrieves, calls the model exactly once, verifies and abstains."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    _with_member_regions,
    select_context,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.hybrid_search import FusedHit
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.query_translation import QueryTranslationDTO
from enterprise_pdf_rag.adapters.tree_retrieval import TreeRouteDTO
from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerRequest,
    AnswerResult,
    AnswerStatus,
    ClaimKind,
    MemberFilters,
    PageWindowStat,
    TranslatedQuery,
    TreeRoute,
)
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, ModelClaim
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import BlockKind
from enterprise_pdf_rag.processing.document_tree import (
    DOCUMENT_TREE_SCHEMA,
    DocumentTree,
    TreeNode,
)
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
    SNAPSHOT,
    FakeDocument,
    FakeMember,
    diagram_ir,
    donut_chart,
    formula_ir,
    pending_chart,
)
from tests.enterprise_pdf_rag.answers.fake_llm import (
    Router,
    Script,
    Translator,
    answered,
    chart_claim,
    declined,
    scripted_client,
)
from tests.enterprise_pdf_rag.answers.store_mounted_document import (
    StoreMountedDocument,
    bar_document,
)

_MEMBER_LINE = re.compile(r"^\[(?:m\d+ \| )?member ([0-9a-f]{64})\] kind=(\w+)", re.MULTILINE)
_ALIAS_LINE = re.compile(r"^\[(m\d+) \| member (\S+)\] kind=", re.MULTILINE)
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
    translator: Translator | None = None,
    router: Router | None = None,
    trees: Mapping[str, DocumentTree] | None = None,
    max_live_calls: int = 1,
) -> tuple[AnswerService, list[str]]:
    client, prompts = scripted_client(
        tmp_path / "llm",
        script,
        max_live_calls=max_live_calls,
        translator=translator,
        router=router,
    )
    service = AnswerService(
        {document.source_sha256: document},
        client,
        settings=settings,
        reranker=reranker,
        trees=trees,
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
        blocks = prompt.split("| member ")[1:]
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
        blocks = prompt.split("| member ")[1:]
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
        blocks = prompt.split("| member ")[1:]
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


# One channel ranking a visual object high while the other never scores it is exactly what
# RRF buries: the real p.6 diagram sat at vector rank 12 with no lexical rank at all, and
# fusion sorted it to rank 30, below every hit both channels contributed to.
_TOP_K = 10
_FUSED_TEXTS = tuple(f"t{index:02d}" for index in range(29))


def _fused_hit(
    member_id: str, score: float, *, vector: int | None, lexical: int | None
) -> FusedHit:
    return FusedHit(SNAPSHOT, member_id, score, vector, lexical, None, None)


def _buried_ranking(
    *buried: tuple[str, int | None, int | None], seated: tuple[str, ...] = ()
) -> tuple[FusedHit, ...]:
    """A ranking whose head both channels rank; each ``buried`` row sits past ``2 * top_k``.

    A row is (member id, vector rank, lexical rank) and lands at fused rank 30 or below,
    carrying only the channel ranks it is given.
    """
    ahead = [*seated, *_FUSED_TEXTS][: len(_FUSED_TEXTS)]
    ranked = [
        _fused_hit(member_id, 1.0 - 0.001 * position, vector=position + 1, lexical=position + 1)
        for position, member_id in enumerate(ahead)
    ]
    ranked += [
        _fused_hit(member_id, 0.0139 - 0.0001 * position, vector=vector, lexical=lexical)
        for position, (member_id, vector, lexical) in enumerate(buried)
    ]
    return tuple(ranked)


def _buried_document(*visual: FakeMember) -> FakeDocument:
    texts = tuple(
        FakeMember(member_id, f"Narrative sentence {member_id}") for member_id in _FUSED_TEXTS
    )
    # The vector order goes unused: these tests hand ``select_context`` a ranking directly.
    return FakeDocument((*texts, *visual), ())


def _proven_diagram(member_id: str) -> FakeMember:
    return FakeMember(member_id, "Agency technology investment", visual=diagram_ir())


@pytest.mark.parametrize(("vector", "lexical"), [(12, None), (None, 12)])
def test_a_visual_one_channel_ranks_high_is_seated_though_fusion_buried_it(
    vector: int | None, lexical: int | None
) -> None:
    document = _buried_document(_proven_diagram("diagram"))
    fused, blocks = select_context(
        document, _buried_ranking(("diagram", vector, lexical)), _TOP_K, document.member_texts()
    )
    assert [hit.member_id for hit in fused] == [*_FUSED_TEXTS[: _TOP_K - 1], "diagram"]
    assert [block.member_id for block in blocks][-1] == "diagram"


def test_no_seat_is_given_outside_both_the_fused_window_and_either_channel_window() -> None:
    document = _buried_document(_proven_diagram("diagram"))
    fused, _ = select_context(
        document,
        _buried_ranking(("diagram", 2 * _TOP_K + 1, None)),
        _TOP_K,
        document.member_texts(),
    )
    assert [hit.member_id for hit in fused] == list(_FUSED_TEXTS[:_TOP_K])
    assert "diagram" not in document.resolved  # not even read


def test_a_buried_visual_stays_buried_when_its_kind_already_holds_a_seat() -> None:
    document = _buried_document(_proven_diagram("diagram"), _proven_diagram("diagram-2"))
    fused, _ = select_context(
        document,
        _buried_ranking(("diagram-2", 12, None), seated=("diagram",)),
        _TOP_K,
        document.member_texts(),
    )
    assert [hit.member_id for hit in fused] == ["diagram", *_FUSED_TEXTS[: _TOP_K - 1]]
    assert "diagram-2" not in document.resolved


def test_several_buried_visuals_are_offered_in_fused_order() -> None:
    # Fused order decides which one is read, not the member id and not the better channel rank.
    document = _buried_document(_proven_diagram("diagram"), _proven_diagram("diagram-2"))
    fused, _ = select_context(
        document,
        _buried_ranking(("diagram-2", 8, None), ("diagram", 3, None)),
        _TOP_K,
        document.member_texts(),
    )
    assert fused[-1].member_id == "diagram-2"
    assert "diagram" not in document.resolved

    swapped = _buried_document(_proven_diagram("diagram"), _proven_diagram("diagram-2"))
    reordered, _ = select_context(
        swapped,
        _buried_ranking(("diagram", 3, None), ("diagram-2", 8, None)),
        _TOP_K,
        swapped.member_texts(),
    )
    assert reordered[-1].member_id == "diagram"
    assert "diagram-2" not in swapped.resolved


def test_the_service_offers_the_whole_fused_ranking_to_the_seats(tmp_path: Path) -> None:
    """End to end: the diagram only the vector channel ranks is still seated (ADR 0012)."""
    document = _seat_document(
        ("text-1", "text-2", "text-3", "diagram", "text-4", "text-5", "text-6")
    )
    service, prompts = _service(tmp_path, document, lambda prompt: declined())
    # Every text member matches the question lexically; the diagram matches neither word, so
    # fusion sorts it below all six though the vector channel ranked it fourth.
    result = service.answer(AnswerRequest("narrative sentence", top_k=2, fusion_mode="rrf"))
    assert result.fusion_mode == "rrf"
    assert result.member_ids == ("text-1", "diagram")
    assert f"nodes.n1.label: {DIAGRAM_LABELS[0]}" in prompts[0]


def _metadata_document(vector_order: tuple[str, ...]) -> FakeDocument:
    # Page metadata is a page-wide fact, so each member sits on a page of its own here.
    members = (
        FakeMember(
            "cover",
            "ACME 2026 Interim Results",
            page_index=0,
            page_title="ACME 2026 Interim Results",
            page_type="cover",
            periods=("Y2026",),
        ),
        FakeMember(
            "hk",
            "Hong Kong VONB grew strongly",
            page_index=1,
            page_title="Hong Kong",
            page_type="text",
            periods=("1H2026",),
            regions=("Hong Kong",),
        ),
        FakeMember(
            "th",
            "Thailand VONB margin expanded",
            page_index=2,
            page_title="Thailand",
            page_type="text",
            periods=("1H2026",),
            regions=("Thailand",),
        ),
        FakeMember(
            "fy",
            "Full year VONB summary",
            page_index=3,
            page_title="Group overview",
            page_type="text",
            periods=("FY2024",),
            regions=("Group",),
        ),
        FakeMember("bare", "Untagged VONB remarks", page_index=4),
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


# A page of side-by-side charts is the case page metadata alone cannot serve: AIA p.13
# prints three `VONB ($m)` charts under their own headings, every one of them carrying the
# same page-wide regions. Only the per-member binding the page geometry proves
# (``MemberText.member_regions``) says which column a block came from.
_PAGE_REGIONS = ("ASEAN", "AIA Thailand")
_SIDE_BY_SIDE_ORDER = ("asean", "thailand", "group")
_BOUND_COLUMNS = {"asean": ("ASEAN",), "thailand": ("AIA Thailand",)}
_REGION_QUESTION = "VONB"


class _ColumnBoundDocument(FakeDocument):
    """A ``FakeDocument`` whose layout bound some of its members to one column of a page.

    ``FakeMember.regions`` is the page-wide value every member of the page shares;
    ``bound`` is the narrower per-member value ``bind_columns`` proves at mount, and only
    that one may reach a block header.
    """

    def __init__(
        self,
        members: tuple[FakeMember, ...],
        vector_order: tuple[str, ...],
        bound: Mapping[str, tuple[str, ...]],
    ) -> None:
        super().__init__(members, vector_order)
        self._bound = bound

    def member_texts(self) -> tuple[MemberText, ...]:
        return tuple(
            replace(text, member_regions=self._bound.get(text.member_id, ()))
            for text in super().member_texts()
        )


def _side_by_side_members() -> tuple[FakeMember, ...]:
    return (
        FakeMember(
            "asean",
            "VONB ($m)",
            chart=donut_chart(("Agency", "72"), ("Partnerships", "28")),
            page_index=12,
            page_title="VONB by segment",
            regions=_PAGE_REGIONS,
        ),
        FakeMember(
            "thailand",
            "VONB ($m)",
            chart=donut_chart(("Agency", "61"), ("Partnerships", "39")),
            page_index=12,
            page_title="VONB by segment",
            regions=_PAGE_REGIONS,
        ),
        FakeMember(
            "group",
            "VONB ($m) for the group as a whole",
            page_index=12,
            page_title="VONB by segment",
            regions=_PAGE_REGIONS,
        ),
    )


def _bound_document(bound: Mapping[str, tuple[str, ...]]) -> _ColumnBoundDocument:
    return _ColumnBoundDocument(_side_by_side_members(), _SIDE_BY_SIDE_ORDER, bound)


def _block_header(prompt: str, member_id: str) -> str:
    (header,) = [
        line
        for line in prompt.splitlines()
        if line.startswith("[") and f"member {member_id}]" in line
    ]
    return header


def _region_ranking(members: tuple[MemberText, ...]) -> tuple[FusedHit, ...]:
    return tuple(
        _fused_hit(member.member_id, 1.0 - 0.01 * rank, vector=rank + 1, lexical=rank + 1)
        for rank, member in enumerate(members)
    )


def test_a_member_bound_to_a_column_prints_that_region_in_its_block_header(
    tmp_path: Path,
) -> None:
    document = _bound_document(_BOUND_COLUMNS)
    service, prompts = _service(tmp_path, document, lambda prompt: declined())

    service.answer(AnswerRequest(_REGION_QUESTION, top_k=3))

    (prompt,) = prompts
    assert _block_header(prompt, "asean").endswith(" regions=ASEAN")
    assert _block_header(prompt, "thailand").endswith(" regions=AIA Thailand")
    # The neighbouring column's heading never leaks into a block that is not in it...
    assert "AIA Thailand" not in _block_header(prompt, "asean")
    # ...and a member the layout could not bind prints no regions at all.
    assert "regions=" not in _block_header(prompt, "group")
    assert [line for line in prompt.splitlines() if "regions=" in line] == [
        _block_header(prompt, "asean"),
        _block_header(prompt, "thailand"),
    ]


def test_stamping_the_regions_leaves_an_unbound_block_untouched_and_keeps_the_order() -> None:
    document = _bound_document(_BOUND_COLUMNS)
    members = document.member_texts()

    _, blocks = select_context(document, _region_ranking(members), len(members), members)
    stamped = _with_member_regions(blocks, members)

    assert [block.member_id for block in stamped] == [block.member_id for block in blocks]
    assert {block.member_id: block.regions for block in stamped} == {
        "asean": ("ASEAN",),
        "thailand": ("AIA Thailand",),
        "group": (),
    }
    # An unbound member's block is not even copied, and the blocks handed in are unchanged.
    for before, after in zip(blocks, stamped, strict=True):
        assert (after is before) == (before.member_id == "group")
    assert all(block.regions == () for block in blocks)


def test_a_document_no_column_binding_reaches_answers_exactly_as_it_did_before(
    tmp_path: Path,
) -> None:
    # Behaviour parity: where the page geometry named no column, the new field is inert —
    # same prompt, byte for byte, and the same result.
    members = _side_by_side_members()
    baseline, base_prompts = _service(
        tmp_path / "plain", FakeDocument(members, _SIDE_BY_SIDE_ORDER), lambda prompt: declined()
    )
    before = baseline.answer(AnswerRequest(_REGION_QUESTION, top_k=3))
    service, prompts = _service(
        tmp_path / "unbound",
        _ColumnBoundDocument(members, _SIDE_BY_SIDE_ORDER, {}),
        lambda prompt: declined(),
    )

    after = service.answer(AnswerRequest(_REGION_QUESTION, top_k=3))

    assert prompts == base_prompts and "regions=" not in prompts[0]
    assert (after.status, after.member_ids, after.abstain_reason) == (
        before.status,
        before.member_ids,
        before.abstain_reason,
    )


def test_the_system_rules_bind_a_named_region_to_the_block_that_prints_it() -> None:
    # Rule 8 is the whole defence against choosing among three identical `VONB ($m)`
    # headers; pin the clauses that carry the contract, not the paragraph's wording.
    assert "the block whose `regions=` names it and from no other" in SYSTEM_RULES
    assert "whatever language the question and the region are written in" in SYSTEM_RULES
    assert "abstain rather than pick one" in SYSTEM_RULES


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


_PAGE_QUESTION = "zzz-nothing-matches"
_PAGE_ORDER = ("hit-a", "hit-b", "near", "far", "far-near")


def _page_document(vector_order: tuple[str, ...] = _PAGE_ORDER) -> FakeDocument:
    """Two hits and one neighbour on page four; a second page with a neighbour of its own."""
    members = (
        FakeMember("hit-a", "Operating profit rose.", page_index=4, page_title="Group performance"),
        FakeMember(
            "hit-b", "VONB grew over the period.", page_index=4, page_title="Group performance"
        ),
        FakeMember(
            "near",
            "Costs fell 4.2% over the period.",
            page_index=4,
            page_title="Group performance",
        ),
        FakeMember("far", "The notes open here.", page_index=9, page_title="Notes"),
        FakeMember(
            "far-near", "A note on the basis of preparation.", page_index=9, page_title="Notes"
        ),
    )
    return FakeDocument(members, vector_order)


def _quote(member_id: str, text: str, *, claim_id: str = "c1") -> ModelClaim:
    return ModelClaim(
        claim_id=claim_id,
        member_id=member_id,
        kind="quote",
        field_path=f"fragments.{member_id}-span",
        text=text,
    )


def _page_script(answer: str, *claims: ModelClaim) -> Script:
    return lambda prompt: answered(answer, *claims)


def _chunks(prompt: str, prefix: str) -> list[str]:
    return [part for part in prompt.split("\n\n") if part.startswith(prefix)]


def test_two_hits_on_one_page_share_a_single_page_context_block(tmp_path: Path) -> None:
    document = _page_document()
    service, prompts = _service(
        tmp_path, document, _page_script("Profit rose.", _quote("hit-a", "Operating profit rose."))
    )
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert result.status is AnswerStatus.ANSWERED, result
    assert result.member_ids == ("hit-a", "hit-b")
    assert prompts[0].count("[page_context page_index=4]") == 1
    assert "[page_context page_index=9]" not in prompts[0]  # nobody hit that page
    assert "- (text) Costs fell 4.2% over the period." in prompts[0]
    # A hit's own evidence is its member block; the page context never repeats it.
    assert prompts[0].count("Operating profit rose.") == 1
    (window,) = result.page_windows
    assert (window.page_index, window.member_count, window.truncated) == (4, 1, False)
    (chunk,) = _chunks(prompts[0], "[page_context")
    assert window.chars == len(chunk)


def test_page_windows_report_every_page_block_that_reached_the_prompt(tmp_path: Path) -> None:
    document = _page_document(("hit-a", "far", "hit-b", "near", "far-near"))
    service, prompts = _service(
        tmp_path, document, _page_script("Profit rose.", _quote("hit-a", "Operating profit rose."))
    )
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert result.status is AnswerStatus.ANSWERED, result
    assert result.member_ids == ("hit-a", "far")
    assert [window.page_index for window in result.page_windows] == [4, 9]
    assert [window.member_count for window in result.page_windows] == [2, 1]
    assert [window.truncated for window in result.page_windows] == [False, False]
    assert [len(chunk) for chunk in _chunks(prompts[0], "[page_context")] == [
        window.chars for window in result.page_windows
    ]
    assert result.page_windows == tuple(
        PageWindowStat(window.page_index, window.member_count, window.chars, window.truncated)
        for window in result.page_windows
    )


def test_a_tight_prompt_budget_gives_up_the_page_context_before_a_hit(tmp_path: Path) -> None:
    document = _page_document()
    script = _page_script("Profit rose.", _quote("hit-a", "Operating profit rose."))
    whole, prompts = _service(tmp_path / "whole", document, script)
    assert whole.answer(AnswerRequest(_PAGE_QUESTION, top_k=2)).page_windows != ()
    hits_only = sum(len(chunk) for chunk in _chunks(prompts[0], "[m"))  # "[m1 | member …"

    service, tight = _service(
        tmp_path / "tight",
        document,
        script,
        settings=AnswerSettings(prompt_budget_chars=hits_only),
    )
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert result.status is AnswerStatus.ANSWERED, result
    assert result.member_ids == ("hit-a", "hit-b")  # both hits survive
    assert "[page_context" not in tight[0] and result.page_windows == ()


def test_the_page_window_switch_lives_in_the_settings_and_the_request(tmp_path: Path) -> None:
    document = _page_document()
    script = _page_script("Profit rose.", _quote("hit-a", "Operating profit rose."))
    off, off_prompts = _service(
        tmp_path / "off", document, script, settings=AnswerSettings(page_window=False)
    )
    assert off.answer(AnswerRequest(_PAGE_QUESTION, top_k=2)).page_windows == ()
    assert "[page_context" not in off_prompts[0]

    asked_off, off_by_request = _service(tmp_path / "request-off", document, script)
    assert (
        asked_off.answer(AnswerRequest(_PAGE_QUESTION, top_k=2, page_window=False)).page_windows
        == ()
    )
    assert "[page_context" not in off_by_request[0]

    asked_on, on_by_request = _service(
        tmp_path / "request-on", document, script, settings=AnswerSettings(page_window=False)
    )
    assert (
        asked_on.answer(AnswerRequest(_PAGE_QUESTION, top_k=2, page_window=True)).page_windows != ()
    )
    assert "[page_context page_index=4]" in on_by_request[0]


def test_a_number_only_in_the_page_context_no_longer_abstains_the_answer(tmp_path: Path) -> None:
    document = _page_document()
    script = _page_script(
        "Operating profit rose while costs fell 4.2%.", _quote("hit-a", "Operating profit rose.")
    )
    service, _ = _service(tmp_path / "on", document, script)
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert result.status is AnswerStatus.ANSWERED, result
    # The figure is repeatable but still uncited: only the quote claim carries a citation.
    assert [claim.claim_id for claim in result.claims] == ["c1"]
    assert all("4.2" not in claim.text for claim in result.claims)

    closed, _ = _service(
        tmp_path / "off", document, script, settings=AnswerSettings(page_window=False)
    )
    shut = closed.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert (shut.status, shut.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
    )
    assert shut.abstain_detail is not None and "4.2%" in shut.abstain_detail


def test_a_claim_naming_a_page_context_member_is_rejected_as_an_unknown_member(
    tmp_path: Path,
) -> None:
    document = _page_document()
    script = _page_script("Costs fell 4.2%.", _quote("near", "Costs fell 4.2% over the period."))
    service, prompts = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert "member near]" not in prompts[0]  # printed as page context only
    (rejected,) = result.rejected
    assert (rejected.claim_id, rejected.member_id) == ("c1", "near")
    assert rejected.reason is AbstainReason.MODEL_OUTPUT_INVALID
    assert rejected.detail == "unknown member"
    assert (result.status, result.abstain_reason, result.claims) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_OUTPUT_INVALID,
        (),
    )


def test_the_prompt_numbers_the_member_blocks_m1_upward_in_printed_order(tmp_path: Path) -> None:
    document = _page_document()
    script = _page_script("Profit rose.", _quote("hit-a", "Operating profit rose."))
    service, prompts = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest(_PAGE_QUESTION, top_k=2))
    assert result.status is AnswerStatus.ANSWERED, result
    # Only the citable blocks are numbered, so the page context on the same page gets none.
    assert _ALIAS_LINE.findall(prompts[0]) == [("m1", "hit-a"), ("m2", "hit-b")]
    assert "[page_context page_index=4]" in prompts[0]


def test_a_claim_naming_a_block_by_its_alias_answers_exactly_as_the_full_member_id_does(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, chart_member = bar

    def by_alias(prompt: str) -> ModelAnswer:
        aliases = {member: alias for alias, member in _ALIAS_LINE.findall(prompt)}
        return answered(
            "The expense ratio in 1H21 was 15%.",
            chart_claim(aliases[chart_member], "p-1H21", "15%"),
        )

    aliased, _ = _service(tmp_path / "alias", document, by_alias)
    full, _ = _service(tmp_path / "full", document, _one_chart_claim("15%"))

    result = aliased.answer(AnswerRequest(_QUESTION))

    assert result.status is AnswerStatus.ANSWERED and result.rejected == ()
    assert result == full.answer(AnswerRequest(_QUESTION))
    # The alias lives in the prompt alone: the report and the citation name the real member.
    assert chart_member in result.member_ids
    assert {citation.member_id for claim in result.claims for citation in claim.citations} == {
        chart_member
    }


def test_a_claim_naming_an_alias_no_block_carries_is_rejected_as_an_unknown_member(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    script = _page_script("It was 15%.", chart_claim("m99", "p-1H21", "15%"))
    service, prompts = _service(tmp_path, document, script)
    result = service.answer(AnswerRequest(_QUESTION))
    assert "m99" not in prompts[0]  # fewer blocks than that were ever offered
    (rejected,) = result.rejected
    assert (rejected.claim_id, rejected.member_id) == ("c1", "m99")
    assert rejected.reason is AbstainReason.MODEL_OUTPUT_INVALID
    assert rejected.detail == "unknown member"
    assert (result.status, result.abstain_reason, result.claims) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_OUTPUT_INVALID,
        (),
    )


_CHINESE_QUESTION = "1H21 的费用率是多少？"  # noqa: RUF001 — a real Chinese question ends in the fullwidth mark
_CHINESE_ENGLISH = "expense ratio 1H21"


def _translator(calls: list[str], english: str = _CHINESE_ENGLISH) -> Translator:
    def translate(prompt: str) -> QueryTranslationDTO:
        calls.append(prompt)
        return QueryTranslationDTO(english_query=english, source_language="Chinese")

    return translate


def test_a_short_question_is_answered_from_the_lexical_channel_alone(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, chart_member = bar
    service, prompts = _service(tmp_path, document, _one_chart_claim("15%"))

    result = service.answer(AnswerRequest("expense ratio 1H21"))

    assert result.status is AnswerStatus.ANSWERED
    assert result.fusion_mode == "bm25_only"
    assert result.query_translation is None
    assert chart_member in result.member_ids
    assert all(hit.vector_rank is None for hit in result.fused)
    assert len(prompts) == 1 and result.llm_live_calls == 1


def test_a_narrative_question_keeps_both_channels(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, _ = _service(tmp_path, document, _one_chart_claim("15%"))
    result = service.answer(AnswerRequest(_QUESTION))
    assert result.fusion_mode == "rrf"
    assert any(hit.vector_rank is not None for hit in result.fused)


def test_an_explicit_fusion_mode_overrides_the_classifier(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, _ = _service(tmp_path, document, _one_chart_claim("15%"))
    result = service.answer(AnswerRequest("expense ratio 1H21", fusion_mode="rrf"))
    assert result.fusion_mode == "rrf"
    assert any(hit.vector_rank is not None for hit in result.fused)


def test_a_chinese_question_is_translated_once_and_then_retrieved_lexically(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, chart_member = bar
    translations: list[str] = []
    service, prompts = _service(
        tmp_path,
        document,
        _one_chart_claim("15%"),
        translator=_translator(translations),
        max_live_calls=2,
    )

    request = AnswerRequest(_CHINESE_QUESTION)
    result = service.answer(request)

    assert result.status is AnswerStatus.ANSWERED
    assert result.query_translation == TranslatedQuery(_CHINESE_ENGLISH, "Chinese", cache_hit=False)
    assert result.fusion_mode == "bm25_only"
    assert chart_member in result.member_ids
    # One translation call plus one synthesis call, both live, both counted.
    assert len(translations) == 1 and _CHINESE_QUESTION in translations[0]
    assert len(prompts) == 1 and result.llm_live_calls == 2
    # The prompt carries the question the user asked, not the translation.
    assert _CHINESE_QUESTION in prompts[0] and _CHINESE_ENGLISH not in prompts[0]

    # Both calls replay from the immutable cache on a repeat.
    again = service.answer(request)
    assert again.llm_live_calls == 0 and len(translations) == 1 and len(prompts) == 1
    assert again.query_translation is not None and again.query_translation.cache_hit is True


def test_an_english_question_is_never_translated(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    translations: list[str] = []
    service, _ = _service(
        tmp_path, document, _one_chart_claim("15%"), translator=_translator(translations)
    )
    result = service.answer(AnswerRequest(_QUESTION))
    assert translations == [] and result.query_translation is None
    assert result.llm_live_calls == 1


def test_a_chinese_question_naming_a_period_is_still_translated(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    """``1H21`` matches the index on its own, so the probe must ignore figures."""
    translations: list[str] = []
    document, _ = bar
    service, _ = _service(
        tmp_path,
        document,
        _one_chart_claim("15%"),
        translator=_translator(translations),
        max_live_calls=2,
    )
    assert "1h21" in _CHINESE_QUESTION.lower()
    service.answer(AnswerRequest(_CHINESE_QUESTION))
    assert len(translations) == 1


def test_an_unusable_translation_falls_back_to_the_vector_channel_alone(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    service, prompts = _service(
        tmp_path,
        document,
        _one_chart_claim("15%"),
        translator=lambda prompt: "not a translation at all",
        max_live_calls=2,
    )
    result = service.answer(AnswerRequest(_CHINESE_QUESTION))
    assert result.status is AnswerStatus.ANSWERED
    assert result.query_translation is None
    # Nothing worth fusing a foreign question with, so the vector channel answers alone.
    assert result.fusion_mode == "vector_only"
    assert all(hit.lexical_rank is None for hit in result.fused)
    assert len(prompts) == 1


def test_translation_can_be_switched_off_per_request(
    tmp_path: Path, bar: tuple[StoreMountedDocument, str]
) -> None:
    document, _ = bar
    translations: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        _one_chart_claim("15%"),
        translator=_translator(translations),
        max_live_calls=2,
    )
    result = service.answer(AnswerRequest(_CHINESE_QUESTION, translate_query=False))
    assert translations == [] and result.query_translation is None
    assert result.fusion_mode == "vector_only"
    assert result.llm_live_calls == 1


_STAGES_QUESTION = "代理人科技投入的三个阶段分别是什么？"  # noqa: RUF001 — a real Chinese question ends in the fullwidth mark
_STAGES_ENGLISH = "What are the three stages of the agents' technology investment?"


def test_only_the_lexical_channel_reads_the_translation(tmp_path: Path) -> None:
    """The restatement is a lexical device: the vector channel keeps the question asked."""
    document = FakeDocument(
        (
            FakeMember("agency", "Agency technology investment ran in three stages"),
            FakeMember("other", "Unrelated closing remarks"),
        ),
        ("other", "agency"),
    )
    translations: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        translator=_translator(translations, _STAGES_ENGLISH),
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_STAGES_QUESTION, top_k=2))

    assert len(translations) == 1
    assert result.query_translation == TranslatedQuery(_STAGES_ENGLISH, "Chinese", cache_hit=False)
    assert result.fusion_mode == "rrf"
    # The embedder was handed the Chinese question verbatim, wording and all...
    assert [query for query, _ in document.search_calls] == [_STAGES_QUESTION]
    # ...while BM25, which cannot score a Chinese token, scored the English restatement.
    assert {hit.member_id for hit in result.fused if hit.lexical_rank is not None} == {"agency"}
    assert {hit.member_id for hit in result.fused if hit.vector_rank is not None} == {
        "agency",
        "other",
    }


_CHINESE_REGION_QUESTION = "泰国的新业务价值是多少？"  # noqa: RUF001 — a real Chinese question ends in the fullwidth mark
_ENGLISH_REGION_QUESTION = "Thailand VONB in 1H26"
_CHINESE_YEAR_REGION_QUESTION = "2026年泰国的新业务价值是多少？"  # noqa: RUF001 — see above


def test_the_translation_also_derives_the_pre_filters(tmp_path: Path) -> None:
    document = _metadata_document(("bare", "cover", "hk", "th", "fy"))
    translations: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        translator=_translator(translations, _ENGLISH_REGION_QUESTION),
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_CHINESE_REGION_QUESTION, top_k=1))

    assert len(translations) == 1
    # The Chinese question names no vocabulary region on its own; the translation does.
    assert result.filters_applied == MemberFilters(("1H2026",), ("Thailand",))
    assert result.filters_relaxed is False
    assert result.member_ids == ("th",)


def test_the_union_keeps_what_the_question_itself_derived(tmp_path: Path) -> None:
    document = _metadata_document(("bare", "cover", "hk", "th", "fy"))
    translations: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        translator=_translator(translations, "Thailand VONB"),
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_CHINESE_YEAR_REGION_QUESTION, top_k=1))

    assert len(translations) == 1
    assert result.filters_applied == MemberFilters(("Y2026",), ("Thailand",))
    assert result.member_ids == ("th",)


def test_an_explicit_filter_is_never_widened_by_the_translation(tmp_path: Path) -> None:
    document = _metadata_document(("bare", "cover", "hk", "th", "fy"))
    translations: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        translator=_translator(translations, _ENGLISH_REGION_QUESTION),
        max_live_calls=2,
    )

    result = service.answer(
        AnswerRequest(
            _CHINESE_REGION_QUESTION, top_k=1, filters=MemberFilters(regions=("Hong Kong",))
        )
    )

    assert len(translations) == 1  # translated for the channels, as ever
    assert result.filters_applied == MemberFilters((), ("Hong Kong",))
    assert result.filters_relaxed is False
    assert result.member_ids == ("hk",)


_TREE_QUESTION = "How did the agency channel's technology investment progress over the year?"
_TREE_LABEL_QUESTION = "agency investment"
_TREE_SHA = "d" * 64  # ``FakeDocument.source_sha256``
_TREE_RATIONALE = "The back matter defines the term."


def _tree_document() -> FakeDocument:
    """Four one-member pages; ``glossary`` (page 3) is ranked by neither fragment channel."""
    return FakeDocument(
        (
            FakeMember("overview", "Agency technology investment overview", page_index=0),
            FakeMember("progress", "Progress of the agency channel", page_index=1),
            FakeMember("noise", "Unrelated closing remarks", page_index=2),
            FakeMember("glossary", "Glossary of abbreviations", page_index=3),
        ),
        ("overview", "progress", "noise"),
    )


def _document_tree() -> DocumentTree:
    """One root per page group; no node title repeats a member's text, so the two are told apart."""
    return DocumentTree(
        DOCUMENT_TREE_SCHEMA,
        _TREE_SHA,
        "sections",
        (
            TreeNode("n1", "Agency channel", 1, (0, 1)),
            TreeNode("n2", "Closing remarks", 1, (2,)),
            TreeNode("n3", "Back matter", 1, (3,)),
        ),
    )


def _router(calls: list[str], node_ids: tuple[str, ...] = ("n3",)) -> Router:
    def route(prompt: str) -> TreeRouteDTO:
        calls.append(prompt)
        return TreeRouteDTO(node_ids=node_ids, pages=(), rationale=_TREE_RATIONALE)

    return route


def test_a_narrative_question_is_routed_and_the_routed_pages_reach_the_prompt(
    tmp_path: Path,
) -> None:
    document = _tree_document()
    routes: list[str] = []
    service, prompts = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        router=_router(routes),
        trees={_TREE_SHA: _document_tree()},
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    # One routing call, reading the rendered outline and the question, before the synthesis.
    assert len(routes) == 1 and _TREE_QUESTION in routes[0]
    assert "n3 p4 Back matter" in routes[0]
    assert result.tree_route == TreeRoute(("n3",), (3,), False, _TREE_RATIONALE)
    # A member neither BM25 nor the vector channel could rank still reaches the prompt.
    assert "glossary" in result.member_ids
    (routed,) = [hit for hit in result.fused if hit.member_id == "glossary"]
    assert (routed.vector_rank, routed.lexical_rank, routed.tree_rank) == (None, None, 1)
    # The outline is a map, never evidence: no node title of it reaches the answer prompt.
    assert len(prompts) == 1 and "Back matter" not in prompts[0]
    # The routing call is live and counted beside the synthesis call.
    assert result.llm_live_calls == 2

    # Both calls replay from the immutable cache on a repeat.
    again = service.answer(AnswerRequest(_TREE_QUESTION, top_k=3))
    assert len(routes) == 1 and len(prompts) == 1 and again.llm_live_calls == 0
    assert again.tree_route is not None and again.tree_route.cache_hit is True
    assert again.member_ids == result.member_ids


def test_a_short_label_question_never_spends_a_routing_call(tmp_path: Path) -> None:
    """ADR 0018's shape test, reused: BM25 already matches a label wherever it is printed."""
    document = _tree_document()
    routes: list[str] = []
    service, prompts = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        router=_router(routes),
        trees={_TREE_SHA: _document_tree()},
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_TREE_LABEL_QUESTION, top_k=3))

    assert routes == [] and result.tree_route is None
    assert all(hit.tree_rank is None for hit in result.fused)
    assert "glossary" not in result.member_ids
    assert len(prompts) == 1 and result.llm_live_calls == 1


def test_the_request_pins_the_routing_decision_over_the_shape_rule(tmp_path: Path) -> None:
    trees = {_TREE_SHA: _document_tree()}
    routes: list[str] = []
    off, _ = _service(
        tmp_path / "off",
        _tree_document(),
        lambda prompt: declined(),
        router=_router(routes),
        trees=trees,
        max_live_calls=2,
    )
    narrative = off.answer(AnswerRequest(_TREE_QUESTION, top_k=3, tree_route=False))
    assert routes == [] and narrative.tree_route is None
    assert "glossary" not in narrative.member_ids

    on, _ = _service(
        tmp_path / "on",
        _tree_document(),
        lambda prompt: declined(),
        router=_router(routes),
        trees=trees,
        max_live_calls=2,
    )
    label = on.answer(AnswerRequest(_TREE_LABEL_QUESTION, top_k=3, tree_route=True))
    assert len(routes) == 1 and _TREE_LABEL_QUESTION in routes[0]
    assert label.tree_route is not None and label.tree_route.pages == (3,)
    assert "glossary" in label.member_ids


def test_without_a_mounted_tree_the_channel_does_not_exist(tmp_path: Path) -> None:
    """No tree, no router, a budget of one: the request is the one it always was."""
    document = _tree_document()
    service, prompts = _service(tmp_path, document, lambda prompt: declined())

    result = service.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    assert result.tree_route is None
    assert all(hit.tree_rank is None for hit in result.fused)
    assert "glossary" not in result.member_ids
    assert len(prompts) == 1 and result.llm_live_calls == 1


def test_an_unusable_route_leaves_the_answer_exactly_as_it_was(tmp_path: Path) -> None:
    document = _tree_document()
    unrouted, first = _service(tmp_path / "plain", document, lambda prompt: declined())
    before = unrouted.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    service, prompts = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        router=lambda prompt: "not a route at all",
        trees={_TREE_SHA: _document_tree()},
        max_live_calls=2,
    )
    result = service.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    # The channel is absent, not an error: the other two channels answered alone.
    assert result.tree_route is None
    assert all(hit.tree_rank is None for hit in result.fused)
    assert (result.member_ids, result.abstain_reason) == (before.member_ids, before.abstain_reason)
    assert len(first) == 1 and len(prompts) == 1 and result.llm_live_calls == 2


def test_an_exhausted_call_budget_drops_the_route_and_not_the_answer(tmp_path: Path) -> None:
    """The route is attempted first, so a budget it cannot afford must cost nothing else."""
    document = _tree_document()
    unrouted, first = _service(tmp_path, document, lambda prompt: declined())
    before = unrouted.answer(AnswerRequest(_TREE_QUESTION, top_k=3))
    assert len(first) == 1

    routes: list[str] = []
    spent, second = _service(
        tmp_path,
        document,
        lambda prompt: declined(),
        router=_router(routes),
        trees={_TREE_SHA: _document_tree()},
        max_live_calls=0,
    )
    result = spent.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    assert routes == [] and second == []  # nothing could be sent at all
    assert result.tree_route is None
    assert (result.member_ids, result.abstain_reason) == (before.member_ids, before.abstain_reason)
    assert result.llm_live_calls == 0 and result.cache_hit is True


def test_a_tree_routed_abstention_keeps_its_route(tmp_path: Path) -> None:
    document = _tree_document()
    routes: list[str] = []
    service, _ = _service(
        tmp_path,
        document,
        lambda prompt: "not json at all",
        router=_router(routes),
        trees={_TREE_SHA: _document_tree()},
        max_live_calls=2,
    )

    result = service.answer(AnswerRequest(_TREE_QUESTION, top_k=3))

    assert (result.status, result.abstain_reason) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_OUTPUT_INVALID,
    )
    assert len(routes) == 1
    assert result.tree_route == TreeRoute(("n3",), (3,), False, _TREE_RATIONALE)
    assert result.llm_live_calls == 2
