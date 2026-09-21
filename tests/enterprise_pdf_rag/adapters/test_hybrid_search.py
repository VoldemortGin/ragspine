"""Hybrid search fuses the pinned vector channel with a snapshot-bound BM25 index."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import pytest

from enterprise_pdf_rag.adapters.hybrid_search import (
    FusedHit,
    HybridSearch,
    LexicalIndex,
    LocalRerankJudge,
    SearchOutcome,
    build_lexical_index,
    fuse,
    lexical_rank,
)
from enterprise_pdf_rag.adapters.local_models import RerankResult
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedLookupContext
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingManifest
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from ragspine.retrieval.lexical.retrieval import bm25_scores, rrf_fuse, tokenize
from tests.enterprise_pdf_rag.answers.fake_document import (
    DONUT_TITLE,
    FakeDocument,
    FakeMember,
    donut_chart,
)
from tests.enterprise_pdf_rag.answers.store_mounted_document import bar_document

_SNAPSHOT = "1" * 64
_TEXTS = {
    "m-a": "Revenue expense ratio grew in the period",
    "m-b": "Expense ratio declined for the agency channel",
    "m-c": "Unrelated closing remarks",
    "m-d": "",
}


class _FakeDocument:
    """In-memory stand-in for the mounted document seam; counts corpus reads."""

    def __init__(self, vector_order: tuple[str, ...], *, snapshot_id: str = _SNAPSHOT) -> None:
        self._snapshot_id = snapshot_id
        self._vector_order = vector_order
        self.member_texts_calls = 0
        self.search_calls: list[tuple[str, int]] = []

    @property
    def source_sha256(self) -> str:
        return "d" * 64

    @property
    def processing_id(self) -> str:
        return "p" * 64

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return "fake-fingerprint"

    def manifest(self) -> ProcessingManifest:
        raise AssertionError("manifest is not needed by hybrid search")

    def member_texts(self) -> tuple[MemberText, ...]:
        self.member_texts_calls += 1
        return tuple(
            MemberText(member_id, ObjectKind.TEXT, 0, text)
            for member_id, text in sorted(_TEXTS.items())
        )

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        self.search_calls.append((query, limit))
        return tuple(
            PinnedRetrievalHit(self._snapshot_id, member_id, 1.0 - 0.1 * rank)
            for rank, member_id in enumerate(self._vector_order[:limit])
        )

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        raise AssertionError("resolve is not part of ranking")

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        raise AssertionError("chart evidence is not part of ranking")

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        raise AssertionError("displayed evidence is not part of ranking")


@dataclass
class _CountingJudge:
    """Reverses the fused order and records the candidate texts it was shown."""

    calls: int = 0
    seen: list[list[str]] = field(default_factory=list)

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls += 1
        self.seen.append(list(candidates))
        return list(reversed(range(len(candidates))))


class _ExplodingJudge:
    def judge(self, query: str, candidates: list[str]) -> Never:
        raise AssertionError("the reranker must not be consulted while rerank is off")


def test_fake_document_satisfies_the_port() -> None:
    assert isinstance(_FakeDocument(("m-a",)), MountedDocument)


def test_lexical_rank_is_deterministic_and_matches_ragspine_bm25() -> None:
    index = build_lexical_index(_FakeDocument(()))
    assert isinstance(index, LexicalIndex)
    assert index.snapshot_id == _SNAPSHOT
    assert index.member_ids == ("m-a", "m-b", "m-c", "m-d")
    assert index.docs_tokens[3] == ()  # empty members stay in the corpus with score 0

    ranked = lexical_rank(index, "expense ratio", limit=10)
    expected = bm25_scores(
        tokenize("expense ratio"), [tokenize(_TEXTS[m]) for m in index.member_ids]
    )
    scored = sorted(
        ((score, member_id) for member_id, score in zip(index.member_ids, expected, strict=True)),
        key=lambda item: (-item[0], item[1]),
    )
    assert [hit.member_id for hit in ranked] == [m for score, m in scored if score > 0]
    assert [hit.score for hit in ranked] == [score for score, _ in scored if score > 0]
    assert {hit.member_id for hit in ranked} == {"m-a", "m-b"}  # zero scores are filtered
    assert all(hit.snapshot_id == _SNAPSHOT for hit in ranked)
    assert lexical_rank(index, "expense ratio", limit=1) == ranked[:1]
    assert lexical_rank(index, "zzz-absent", limit=10) == ()
    with pytest.raises(ValueError, match="query"):
        lexical_rank(index, "   ", limit=10)
    with pytest.raises(ValueError, match="limit"):
        lexical_rank(index, "expense", limit=0)


def test_lexical_rank_breaks_score_ties_by_member_id() -> None:
    index = LexicalIndex(_SNAPSHOT, ("m-z", "m-y"), (("ratio",), ("ratio",)))
    ranked = lexical_rank(index, "ratio", limit=5)
    assert [hit.member_id for hit in ranked] == ["m-y", "m-z"]
    assert ranked[0].score == ranked[1].score


def test_lexical_index_id_is_content_addressed_by_snapshot_and_parameters() -> None:
    document = _FakeDocument(())
    base = build_lexical_index(document)
    assert base.index_id == build_lexical_index(document).index_id
    assert base.index_id != build_lexical_index(document, k1=1.2).index_id
    assert base.index_id != build_lexical_index(_FakeDocument((), snapshot_id="2" * 64)).index_id


def test_fuse_matches_ragspine_rrf_and_keeps_channel_ranks() -> None:
    vector = (
        PinnedRetrievalHit(_SNAPSHOT, "m-a", 0.9),
        PinnedRetrievalHit(_SNAPSHOT, "m-b", 0.8),
    )
    lexical = (
        PinnedRetrievalHit(_SNAPSHOT, "m-b", 3.0),
        PinnedRetrievalHit(_SNAPSHOT, "m-c", 1.0),
    )
    fused = fuse(vector, lexical, k=60.0)
    expected = rrf_fuse([["m-a", "m-b"], ["m-b", "m-c"]], 60)
    assert [hit.member_id for hit in fused] == ["m-b", "m-a", "m-c"]
    assert {hit.member_id: hit.fused_score for hit in fused} == expected
    by_id = {hit.member_id: hit for hit in fused}
    assert (by_id["m-b"].vector_rank, by_id["m-b"].lexical_rank) == (2, 1)
    assert (by_id["m-b"].vector_score, by_id["m-b"].bm25_score) == (0.8, 3.0)
    assert (by_id["m-a"].lexical_rank, by_id["m-a"].bm25_score) == (None, None)
    assert (by_id["m-c"].vector_rank, by_id["m-c"].vector_score) == (None, None)
    assert by_id["m-a"].as_hit() == PinnedRetrievalHit(_SNAPSHOT, "m-a", by_id["m-a"].fused_score)
    assert fuse((), ()) == ()


def test_fuse_orders_equal_scores_by_member_id_and_rejects_mixed_snapshots() -> None:
    only_vector = (PinnedRetrievalHit(_SNAPSHOT, "m-z", 0.5),)
    only_lexical = (PinnedRetrievalHit(_SNAPSHOT, "m-y", 0.5),)
    assert [hit.member_id for hit in fuse(only_vector, only_lexical)] == ["m-y", "m-z"]
    with pytest.raises(ValueError, match="snapshot"):
        fuse(only_vector, (PinnedRetrievalHit("2" * 64, "m-y", 0.5),))


def test_hybrid_search_builds_the_lexical_index_once_per_snapshot() -> None:
    document = _FakeDocument(("m-c", "m-a"))
    cache: dict[str, LexicalIndex] = {}
    first = HybridSearch(document, channel_limit=3, index_cache=cache)
    second = HybridSearch(document, channel_limit=3, index_cache=cache)
    assert document.member_texts_calls == 1
    assert len(cache) == 1

    hits = first.search("expense ratio", top_k=2, mode="rrf").hits
    assert document.search_calls == [("expense ratio", 3)]
    assert all(isinstance(hit, FusedHit) for hit in hits)
    # m-a is in both channels, m-c only in the vector channel, m-b only lexically.
    assert [hit.member_id for hit in hits] == ["m-a", "m-c"]
    assert [hit.member_id for hit in first.search("expense ratio", top_k=3, mode="rrf").hits] == [
        "m-a",
        "m-c",
        "m-b",
    ]
    assert second.search("expense ratio", top_k=2, mode="rrf").hits == hits

    other = _FakeDocument(("m-a",), snapshot_id="2" * 64)
    HybridSearch(other, index_cache=cache)
    assert other.member_texts_calls == 1 and len(cache) == 2
    with pytest.raises(ValueError, match="top_k"):
        first.search("expense ratio", top_k=0, mode="rrf")


def test_rerank_is_off_by_default_and_only_an_injected_judge_reorders() -> None:
    document = FakeDocument(
        tuple(FakeMember(member_id, text) for member_id, text in _TEXTS.items()), ("m-a", "m-b")
    )
    plain = HybridSearch(document).search("expense ratio", top_k=3, mode="rrf").hits
    assert [hit.member_id for hit in plain] == ["m-a", "m-b"]
    assert (
        HybridSearch(document, reranker=None).search("expense ratio", top_k=3, mode="rrf").hits
        == plain
    )
    assert document.resolved == []  # no evidence is read while rerank is off

    judge = _CountingJudge()
    reranked = (
        HybridSearch(document, reranker=judge).search("expense ratio", top_k=3, mode="rrf").hits
    )
    assert judge.calls == 1
    assert [hit.member_id for hit in reranked] == ["m-b", "m-a"]
    assert set(reranked) == set(plain)
    # The judge read each fused candidate's evidence block, not the tokenized index text.
    assert document.resolved == ["m-a", "m-b"]
    (seen,) = judge.seen
    assert seen[0].startswith("[member ") and "kind=text" in seen[0]
    assert f"fragments.m-a-span: {_TEXTS['m-a']}" in seen[0]
    assert f"fragments.m-b-span: {_TEXTS['m-b']}" in seen[1]


def test_reranker_reads_a_chart_candidate_as_its_citable_evidence_block(tmp_path: Path) -> None:
    document, pin = bar_document(tmp_path)
    judge = _CountingJudge()
    hits = (
        HybridSearch(document, reranker=judge)
        .search("expense ratio 1H21", top_k=2, mode="rrf")
        .hits
    )
    assert {hit.member_id for hit in hits} == {
        pin.member_id,
        *document.member_ids_by_kind(ObjectKind.TEXT),
    }
    (seen,) = judge.seen
    chart_text = next(text for text in seen if "kind=chart" in text)
    assert "points.p-1H21.value: series=Expense Ratio category=1H21 unit=% value=15" in chart_text
    assert "points.p-1H23.value" in chart_text
    # The lexical corpus scores the projection the displayed-bar admission embedded.
    (bar_text,) = (item for item in document.member_texts() if item.member_id == pin.member_id)
    assert (
        bar_text.text
        == "Expense Ratio bar chart figure 1H21 Expense Ratio 15% 1H23 Expense Ratio 6%"
    )


_LONG_TEXTS = {
    f"text-{index:02d}": sentence
    for index, sentence in enumerate(
        (
            "Premier Agency: 55% of VONB and 18% growth in active agents",
            "Partnerships VONB grew 20% with bancassurance in Hong Kong and Thailand",
            "Record operating ROE of 17.5% in the first half",
            "OPAT per share increased 12% on a constant exchange rate basis",
            "Free surplus generation remained strong across all markets",
            "Chinese Mainland visitor sales were broadly stable year on year",
            "New business margin improved in every reportable segment",
            "Group embedded value rose to a record level at 30 June",
            "Underlying free surplus generation per share up 10%",
            "Shareholder allocated equity increased after the buy-back",
            "Health and protection sales accounted for the majority of ANP",
            "Thailand delivered double-digit growth through the agency channel",
            "The interim dividend per share increased in line with policy",
            "Solvency cover ratio stayed well above the regulatory minimum",
            "Total weighted premium income grew across the portfolio",
            "Expense ratio comparatives are shown on an actual exchange rate basis",
            "Investment income was resilient despite lower interest rates",
            "Renewal premiums drove growth in total weighted premium income",
            "Agent productivity rose with the Premier Agency strategy",
        )
    )
}
_NO_TITLE_QUESTION = "Agency share of VONB 1H26"


def _aia_like(chart_text_only: bool) -> FakeDocument:
    """Nineteen long texts plus one verified donut whose vector rank is 13, as observed on AIA."""
    chart = FakeMember(
        "chart-p18",
        DONUT_TITLE,
        None if chart_text_only else donut_chart(("Agency", "72"), ("Partnerships", "28")),
    )
    texts = tuple(FakeMember(member_id, text) for member_id, text in _LONG_TEXTS.items())
    vector_order = (*tuple(_LONG_TEXTS)[:12], "chart-p18", *tuple(_LONG_TEXTS)[12:])
    return FakeDocument((*texts, chart), vector_order)


def test_projected_chart_text_lets_a_question_without_the_title_reach_the_top() -> None:
    """The observed ISSUE-2 mechanism: title-only index text scores zero lexically for a
    question that names categories, and RRF cannot lift a single-channel rank 13 into the
    top ten; the projection makes the donut the lexical winner instead."""
    before = HybridSearch(_aia_like(chart_text_only=True), channel_limit=50)
    old = before.search(_NO_TITLE_QUESTION, top_k=10, mode="rrf").hits
    assert all(hit.member_id != "chart-p18" for hit in old)
    assert lexical_rank(before.index, _NO_TITLE_QUESTION, limit=50) and all(
        hit.member_id != "chart-p18"
        for hit in lexical_rank(before.index, _NO_TITLE_QUESTION, limit=50)
    )

    after = HybridSearch(_aia_like(chart_text_only=False), channel_limit=50)
    assert lexical_rank(after.index, _NO_TITLE_QUESTION, limit=1)[0].member_id == "chart-p18"
    new = after.search(_NO_TITLE_QUESTION, top_k=10, mode="rrf").hits
    chart = next(hit for hit in new if hit.member_id == "chart-p18")
    assert (chart.vector_rank, chart.lexical_rank) == (13, 1)
    # Lexical rank 1 plus vector rank 13 lands well inside the default top-10 (and the
    # old top-6); a single-channel hit at rank 13 could never have (1/73 < 2/110).
    assert new.index(chart) < 6


def test_search_with_no_hits_in_either_channel_returns_empty() -> None:
    document = _FakeDocument(())
    exploding = HybridSearch(document, reranker=_ExplodingJudge())
    assert exploding.search("zzz-absent", top_k=3, mode="rrf").hits == ()


def test_local_rerank_judge_maps_provider_order_onto_candidate_indices() -> None:
    class _Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[str, ...], int]] = []

        def rerank(
            self, query: str, documents: tuple[str, ...], *, limit: int
        ) -> tuple[RerankResult, ...]:
            self.calls.append((query, documents, limit))
            return (
                RerankResult(index=2, relevance_score=0.9),
                RerankResult(index=0, relevance_score=0.5),
                RerankResult(index=1, relevance_score=0.1),
            )

    adapter = _Adapter()
    judge = LocalRerankJudge(adapter)
    assert judge.judge("q", ["x", "y", "z"]) == [2, 0, 1]
    assert adapter.calls == [("q", ("x", "y", "z"), 3)]
    assert judge.judge("q", []) == []


def test_allowed_members_narrow_both_channels_and_widen_the_vector_read() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b", "m-d"))
    search = HybridSearch(document, channel_limit=2)
    unfiltered = search.search("expense ratio", top_k=4, mode="rrf").hits
    assert [hit.member_id for hit in unfiltered] == ["m-a", "m-c", "m-b"]
    assert document.search_calls[-1] == ("expense ratio", 2)

    narrowed = search.search(
        "expense ratio", top_k=4, allowed=frozenset({"m-b", "m-d"}), mode="rrf"
    ).hits
    # The vector channel is read over the whole corpus (4 members) before filtering.
    assert document.search_calls[-1] == ("expense ratio", 4)
    assert [hit.member_id for hit in narrowed] == ["m-b", "m-d"]
    assert narrowed[0].lexical_rank == 1 and narrowed[0].vector_rank == 1
    assert narrowed[1].lexical_rank is None  # empty text scores 0 lexically
    assert search.search("expense ratio", top_k=4, allowed=frozenset(), mode="rrf").hits == ()
    assert [item.member_id for item in search.index.members] == ["m-a", "m-b", "m-c", "m-d"]


def test_a_short_query_is_routed_to_bm25_alone_and_never_embeds() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    outcome = search.search("expense ratio", top_k=3)
    assert isinstance(outcome, SearchOutcome)
    assert outcome.mode == "bm25_only"
    # The vector channel is the only remote call in the request; a routed query skips it.
    assert document.search_calls == []
    lexical = lexical_rank(search.index, "expense ratio", limit=3)
    assert [hit.member_id for hit in outcome.hits] == [hit.member_id for hit in lexical]
    assert [hit.lexical_rank for hit in outcome.hits] == [1, 2]
    assert all(hit.vector_rank is None and hit.vector_score is None for hit in outcome.hits)
    assert [hit.bm25_score for hit in outcome.hits] == [hit.score for hit in lexical]


def test_a_long_narrative_query_still_fuses_both_channels() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    question = "Which channels drove the change in the expense ratio and why did it move?"
    outcome = search.search(question, top_k=3)
    assert outcome.mode == "rrf"
    assert document.search_calls == [(question, 3)]
    assert outcome.hits == search.search(question, top_k=3, mode="rrf").hits


def test_a_query_the_lexical_channel_cannot_score_takes_the_vector_channel_alone() -> None:
    document = _FakeDocument(("m-c", "m-a"))
    search = HybridSearch(document, channel_limit=3)
    outcome = search.search("中国内地的费用率", top_k=3)
    assert outcome.mode == "vector_only"
    assert [hit.member_id for hit in outcome.hits] == ["m-c", "m-a"]
    assert all(hit.lexical_rank is None and hit.bm25_score is None for hit in outcome.hits)


def test_an_explicit_mode_overrides_the_classifier_in_both_directions() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    # A query ``auto`` would send to BM25 alone, pinned to fusion.
    fused = search.search("expense ratio", top_k=3, mode="rrf")
    assert fused.mode == "rrf" and document.search_calls == [("expense ratio", 3)]
    assert any(hit.vector_rank is not None for hit in fused.hits)

    # A query ``auto`` would fuse, pinned to BM25 alone.
    question = "Which channels drove the change in the expense ratio and why did it move?"
    lexical_only = search.search(question, top_k=3, mode="bm25_only")
    assert lexical_only.mode == "bm25_only"
    assert document.search_calls == [("expense ratio", 3)]  # still no second embedding call
    assert all(hit.vector_rank is None for hit in lexical_only.hits)

    pinned_vector = search.search("expense ratio", top_k=3, mode="vector_only")
    assert pinned_vector.mode == "vector_only"
    assert all(hit.lexical_rank is None for hit in pinned_vector.hits)


def test_single_channel_scores_are_the_fusion_of_that_channel_with_an_empty_one() -> None:
    document = _FakeDocument(("m-c", "m-a"))
    search = HybridSearch(document, channel_limit=3)
    outcome = search.search("expense ratio", top_k=3)
    lexical = lexical_rank(search.index, "expense ratio", limit=3)
    assert outcome.hits == fuse((), lexical, k=60.0)[:3]


def test_lexical_hits_counts_what_bm25_can_score() -> None:
    search = HybridSearch(_FakeDocument(()), channel_limit=50)
    assert search.lexical_hits("expense ratio") == 2
    assert search.lexical_hits("expense ratio", allowed=frozenset({"m-a"})) == 1
    assert search.lexical_hits("中国内地的费用率") == 0
