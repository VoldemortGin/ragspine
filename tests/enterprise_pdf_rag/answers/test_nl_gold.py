"""Replay the frozen natural-language gold set against the real pinned AIA release.

Recall is deliberately **not** under test here. The snapshot's vectors were produced by a
real local embedder the offline gate may not call, so the vector channel is *declared*
rather than computed: it returns exactly the members the case's scripted claims cite. What
this guards is everything else the gold describes — that its anchors (``page_index`` +
``field_path`` + ``quote``) still exist in the pinned evidence, that seat selection and the
context budget put those blocks in the prompt, that field-level verification accepts a
faithful model output and rejects a fabricated one, that the prose numeric gate abstains on
an ungrounded number, and that the pre-filters are derived, applied and relaxed as frozen.

Live recall, latency and cache behaviour are measured by the real-model runner,
``scripts/enterprise_pdf_rag/nl_gold_eval.py``.
"""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.answer_service import AnswerService
from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.http.chat import render_message
from enterprise_pdf_rag.adapters.http.chat_schemas import AnswerEnvelope
from enterprise_pdf_rag.adapters.hybrid_search import LexicalIndex
from enterprise_pdf_rag.adapters.nl_gold import (
    NlGoldCase,
    ObservedAnswer,
    answer_prose,
    judge,
    load_gold,
)
from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.query_translation import QueryTranslationDTO
from enterprise_pdf_rag.answers.models import AnswerRequest, MemberFilters
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.query_mode import FusionMode
from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedLookupContext
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext
from enterprise_pdf_rag.processing.context_builder import ContextBlock, build_context_block
from enterprise_pdf_rag.processing.models import ProcessingManifest
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client

ROOT = Path(__file__).resolve().parents[3]
GOLD_PATH = (
    ROOT
    / "data"
    / "benchmarks"
    / "enterprise-pdf-rag"
    / "aia-2026-interim"
    / "nl-answers-gold-v1.json"
)
GOLD = load_gold(GOLD_PATH.read_bytes())
# The replay declares its vector channel (see ``ReplayMount``), so it pins fusion on:
# channel routing is guarded by ``test_query_mode`` / ``test_hybrid_search`` and measured by
# the live runner, while this runner guards seats, budget, verification and abstention
# (ADR 0016). The scripted translations below stand in for the model's, for the same reason.
_FUSION_MODE: FusionMode = "rrf"
_TRANSLATIONS = {
    "2026 上半年 分销渠道 占比": "Distribution mix 1H26",
    "代理人科技投入的三个阶段分别是什么？": "What are the three phases of agency technology?",  # noqa: RUF001 — a real Chinese question ends in the fullwidth mark
    "泰国 1H26 VONB": "Thailand 1H26 VONB",
}
# A case without scripted model output (the cache repeat) can only run against a service.
OFFLINE_CASES = tuple(case for case in GOLD.cases if case.model_output is not None)

pytestmark = pytest.mark.skipif(
    not (PROCESSING_OUTPUT / "current-processing").is_file(), reason="AIA sample store absent"
)


class _IdentityJudge:
    """A listwise judge that keeps the fused order: rerank runs, nothing is reordered."""

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        return list(range(len(candidates)))


class PinnedRelease:
    """The published release, opened once and read-only, with its resolves memoised.

    ``resolve`` re-proves a member's evidence from the pinned source on every call, which is
    the production invariant and is covered by its own tests. Replaying two dozen gold cases
    would repeat that proof hundreds of times, so this runner resolves each member once.
    """

    def __init__(self, ingestion_root: Path) -> None:
        # Exactly the production mount, evidence-only: the page metadata that feeds the
        # pre-filters and the citation page titles comes with it (ADR 0013).
        catalog = scan_catalog(ingestion_root, legacy_roots=(PROCESSING_OUTPUT,))
        (entry,) = catalog.documents
        self.document = mount_document(entry, embedder=None)
        self.members = self.document.member_texts()
        self.index_cache: dict[str, LexicalIndex] = {}
        self._contexts: dict[str, RetrievalContext] = {}
        self._blocks: dict[str, ContextBlock] = {}

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        context = self._contexts.get(hit.member_id)
        if context is None:
            context = self.document.resolve(hit)
            self._contexts[hit.member_id] = context
        return context

    def block(self, member_id: str) -> ContextBlock:
        block = self._blocks.get(member_id)
        if block is None:
            block = build_context_block(
                self.resolve(
                    PinnedRetrievalHit(self.document.retrieval_snapshot_id, member_id, 1.0)
                )
            )
            self._blocks[member_id] = block
        return block

    def member_of(self, page_index: int, field_path: str) -> str:
        """The pinned member whose evidence block prints ``field_path`` on that page."""
        for member in self.members:
            if member.page_index != page_index:
                continue
            for line in self.block(member.member_id).prompt_text().splitlines():
                if line.startswith(field_path + ":"):
                    return member.member_id
        raise AssertionError(f"The pinned release prints no {field_path} on page {page_index}")


class ReplayMount:
    """``PinnedRelease`` as a ``MountedDocument`` whose vector channel is declared, not run."""

    def __init__(self, release: PinnedRelease, targets: tuple[str, ...]) -> None:
        self._release = release
        self._targets = targets

    @property
    def source_sha256(self) -> str:
        return self._release.document.source_sha256

    @property
    def processing_id(self) -> str:
        return self._release.document.processing_id

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._release.document.retrieval_snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return self._release.document.embedding_fingerprint

    def manifest(self) -> ProcessingManifest:
        return self._release.document.manifest()

    def member_texts(self) -> tuple[MemberText, ...]:
        return self._release.members

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        snapshot = self.retrieval_snapshot_id
        return tuple(
            PinnedRetrievalHit(snapshot, member_id, 1.0 - index / 100)
            for index, member_id in enumerate(self._targets[:limit])
        )

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        return self._release.resolve(hit)

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        return self._release.document.chart_context(hit)

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        return self._release.document.displayed_context(hit)


@pytest.fixture(scope="module")
def release(tmp_path_factory: pytest.TempPathFactory) -> PinnedRelease:
    outputs = ProcessingStore(PROCESSING_OUTPUT)
    processing_id, manifest = outputs.load_current()
    if manifest.retrieval is None:
        pytest.skip("The AIA release has no retrieval snapshot")
    pinned = GOLD.pinned
    if (manifest.scope.source_sha256, processing_id, manifest.retrieval.snapshot_id) != (
        pinned.document_sha256,
        pinned.processing_id,
        pinned.snapshot_id,
    ):
        pytest.skip(
            "The local AIA release is not the one this gold set is pinned to "
            f"(gold {pinned.processing_id[:12]}, store {processing_id[:12]}); "
            "re-freeze the gold against the new release"
        )
    return PinnedRelease(tmp_path_factory.mktemp("no-ingestion"))


def _model_answer(release: PinnedRelease, case: NlGoldCase, prompt: str) -> ModelAnswer:
    """The case's scripted output, with each claim's member resolved from the snapshot."""
    output = case.model_output
    assert output is not None
    claims = []
    for claim in output.claims:
        assert claim.field_path in prompt, (
            f"{case.case_id}: {claim.field_path} did not reach the prompt, "
            "so this replay would not exercise verification"
        )
        claims.append(
            ModelClaim(
                claim_id=claim.claim_id,
                member_id=release.member_of(claim.page_index, claim.field_path),
                kind=claim.kind,
                field_path=claim.field_path,
                text=claim.text,
                row=claim.row,
                col=claim.col,
                header=claim.header,
            )
        )
    return ModelAnswer(
        abstain=output.abstain,
        abstain_reason=output.abstain_reason,
        answer=output.answer,
        claims=tuple(claims),
    )


def _replay(
    release: PinnedRelease, case: NlGoldCase, cache_dir: Path
) -> tuple[ObservedAnswer, str]:
    output = case.model_output
    assert output is not None
    targets = tuple(
        dict.fromkeys(
            release.member_of(claim.page_index, claim.field_path) for claim in output.claims
        )
    )
    document = ReplayMount(release, targets)

    def translate(prompt: str) -> QueryTranslationDTO:
        english = _TRANSLATIONS.get(case.question.text)
        assert english is not None, f"{case.case_id}: no scripted translation for this question"
        return QueryTranslationDTO(english_query=english, source_language="Chinese")

    client, prompts = scripted_client(
        cache_dir,
        lambda prompt: _model_answer(release, case, prompt),
        # A question outside the index's language spends one call translating it first.
        max_live_calls=2,
        translator=translate,
    )
    service = AnswerService(
        {document.source_sha256: document},
        client,
        reranker=_IdentityJudge() if case.request.rerank else None,
        index_cache=release.index_cache,
    )
    filters = case.request.filters
    result = service.answer(
        AnswerRequest(
            case.question.text,
            document_sha256=case.document_sha256,
            rerank=case.request.rerank,
            filters=None if filters is None else MemberFilters(filters.periods, filters.regions),
            fusion_mode=_FUSION_MODE,
        )
    )
    assert len(prompts) == 1, "one bounded synthesis call per answer"
    envelope = AnswerEnvelope.from_domain(result)
    observed = ObservedAnswer.model_validate(envelope.model_dump(mode="json"))
    return observed, answer_prose(render_message(result))


@pytest.mark.parametrize("case", OFFLINE_CASES, ids=[case.case_id for case in OFFLINE_CASES])
def test_gold_case_replays_against_the_pinned_release(
    case: NlGoldCase, release: PinnedRelease, tmp_path: Path
) -> None:
    observed, prose = _replay(release, case, tmp_path)

    # The offline replay scripts its own completion transport, so it cannot observe the
    # service's cache; `cache_hit` belongs to the live runner alone.
    failures = judge(case, observed, prose, skip=frozenset({"cache_hit"}))
    if failures and case.expected.known_gap:
        pytest.skip(f"known gap moved ({case.expected.known_gap_detail}): {'; '.join(failures)}")
    assert failures == (), f"{case.case_id}: " + "; ".join(failures)


def test_every_gold_anchor_is_printed_by_the_pinned_evidence(release: PinnedRelease) -> None:
    """The frozen `page_index` + `field_path` pairs still exist, expectations aside."""
    for case in GOLD.cases:
        for required in case.expected.required_claims:
            if required.field_path is None:
                continue
            member_id = release.member_of(required.page_index, required.field_path)
            printed = release.block(member_id).prompt_text()
            assert required.field_path + ":" in printed
