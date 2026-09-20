"""Hybrid search fuses the pinned vector channel with a snapshot-bound BM25 index."""

from dataclasses import dataclass
from typing import Never

import pytest

from enterprise_pdf_rag.adapters.hybrid_search import (
    FusedHit,
    HybridSearch,
    LexicalIndex,
    LocalRerankJudge,
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
    calls: int = 0

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls += 1
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

    hits = first.search("expense ratio", top_k=2)
    assert document.search_calls == [("expense ratio", 3)]
    assert all(isinstance(hit, FusedHit) for hit in hits)
    # m-a is in both channels, m-c only in the vector channel, m-b only lexically.
    assert [hit.member_id for hit in hits] == ["m-a", "m-c"]
    assert [hit.member_id for hit in first.search("expense ratio", top_k=3)] == [
        "m-a",
        "m-c",
        "m-b",
    ]
    assert second.search("expense ratio", top_k=2) == hits

    other = _FakeDocument(("m-a",), snapshot_id="2" * 64)
    HybridSearch(other, index_cache=cache)
    assert other.member_texts_calls == 1 and len(cache) == 2
    with pytest.raises(ValueError, match="top_k"):
        first.search("expense ratio", top_k=0)


def test_rerank_is_off_by_default_and_only_an_injected_judge_reorders() -> None:
    document = _FakeDocument(("m-a", "m-b"))
    plain = HybridSearch(document).search("expense ratio", top_k=3)
    assert [hit.member_id for hit in plain] == ["m-a", "m-b"]
    assert HybridSearch(document, reranker=None).search("expense ratio", top_k=3) == plain

    judge = _CountingJudge()
    reranked = HybridSearch(document, reranker=judge).search("expense ratio", top_k=3)
    assert judge.calls == 1
    assert [hit.member_id for hit in reranked] == ["m-b", "m-a"]
    assert set(reranked) == set(plain)


def test_search_with_no_hits_in_either_channel_returns_empty() -> None:
    document = _FakeDocument(())
    exploding = HybridSearch(document, reranker=_ExplodingJudge())
    assert exploding.search("zzz-absent", top_k=3) == ()


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
