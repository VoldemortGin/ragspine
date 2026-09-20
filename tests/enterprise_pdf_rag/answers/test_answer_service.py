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
)
from enterprise_pdf_rag.answers.prompt import SYSTEM_RULES, ModelAnswer, ModelClaim
from enterprise_pdf_rag.processing.context_builder import BlockKind
from enterprise_pdf_rag.processing.models import ObjectKind
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    publish_generic_document,
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
    document: StoreMountedDocument,
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
