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
from enterprise_pdf_rag.answers.query_mode import FusionMode
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

    def __init__(
        self,
        vector_order: tuple[str, ...],
        *,
        snapshot_id: str = _SNAPSHOT,
        members: tuple[MemberText, ...] | None = None,
    ) -> None:
        self._snapshot_id = snapshot_id
        self._vector_order = vector_order
        self._members = members
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
        if self._members is not None:
            return self._members
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
    queries: list[str] = field(default_factory=list)

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.calls += 1
        self.seen.append(list(candidates))
        self.queries.append(query)
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


def test_an_empty_tree_ranking_fuses_exactly_as_the_two_channel_call_did() -> None:
    """The third channel is opt-in (ADR 0019): with no pages nothing about the fusion moves."""
    vector = (
        PinnedRetrievalHit(_SNAPSHOT, "m-a", 0.9),
        PinnedRetrievalHit(_SNAPSHOT, "m-b", 0.8),
    )
    lexical = (
        PinnedRetrievalHit(_SNAPSHOT, "m-b", 3.0),
        PinnedRetrievalHit(_SNAPSHOT, "m-c", 1.0),
    )
    two_channel = fuse(vector, lexical, k=60.0)
    assert fuse(vector, lexical, (), k=60.0) == two_channel
    assert all(hit.tree_rank is None for hit in two_channel)
    assert fuse((), (), ()) == ()


def test_a_member_only_the_tree_channel_ranks_enters_the_fusion() -> None:
    vector = (PinnedRetrievalHit(_SNAPSHOT, "m-a", 0.9),)
    tree = (PinnedRetrievalHit(_SNAPSHOT, "m-t", 1.0),)
    fused = fuse(vector, (), tree)
    by_id = {hit.member_id: hit for hit in fused}
    assert set(by_id) == {"m-a", "m-t"}
    assert by_id["m-t"].tree_rank == 1
    assert (by_id["m-t"].vector_rank, by_id["m-t"].lexical_rank) == (None, None)
    # The tree channel is a page set, not a similarity: it carries a rank and no score.
    assert (by_id["m-t"].vector_score, by_id["m-t"].bm25_score) == (None, None)
    # Fused at ``tree_k``, never at ``k``: a routing rank is reading order inside the section
    # the router chose, not a relevance any channel measured.
    assert by_id["m-t"].fused_score == rrf_fuse([["m-t"]], 600)["m-t"]
    assert by_id["m-a"].tree_rank is None
    with pytest.raises(ValueError, match="snapshot"):
        fuse(vector, (), (PinnedRetrievalHit("2" * 64, "m-t", 1.0),))


# ``AnswerRequest.channel_limit``, the depth the service actually runs the channels at: the
# deepest rank either scoring channel can hand ``fuse``, and so the right-hand side of the
# tree constant's inequality.
_CHANNEL_LIMIT = 50


def test_channel_agreement_outranks_depth_in_any_one_scoring_channel() -> None:
    """Ranks are 1-based, so the two *scoring* channels agreeing beats depth in either one:
    at most 1/(60 + 1) alone against at least 2/(60 + 50) shared.

    Until the tree channel was measured this test also claimed the tree as a third peer —
    that a member all three rankings hold outranks a member two of them hold. That contract
    is gone. On the frozen gold set the peer weighting scored 17/22 against 21/22 with the
    channel off, and the five structural questions went 4/5 answered to 3/5, because at the
    shared ``k`` a tree rank-1 member scored 1/61 = 0.01639 and displaced a member only BM25
    could reach at rank 2 (1/62 = 0.01613). A tree rank is reading order inside the section
    the router picked, not a relevance, so it now fuses at its own ``tree_k`` — see
    ``test_a_routed_member_never_outranks_a_member_a_scoring_channel_reached``.
    """
    pair = tuple(
        PinnedRetrievalHit(_SNAPSHOT, member_id, 1.0) for member_id in ("m-both", "m-vector")
    )
    fused = fuse(pair, pair[:1], (pair[0], PinnedRetrievalHit(_SNAPSHOT, "m-tree", 1.0)))
    assert [hit.member_id for hit in fused] == ["m-both", "m-vector", "m-tree"]
    by_id = {hit.member_id: hit for hit in fused}
    assert by_id["m-both"].fused_score > by_id["m-vector"].fused_score
    assert by_id["m-vector"].fused_score > by_id["m-tree"].fused_score
    assert (by_id["m-both"].vector_rank, by_id["m-both"].lexical_rank) == (1, 1)
    assert by_id["m-both"].tree_rank == 1

    # The same property at the depths a real request reaches: a fiftieth seat in two
    # channels still outscores a first seat in one.
    filler = tuple(PinnedRetrievalHit(_SNAPSHOT, f"m-{index:02d}", 1.0) for index in range(49))
    deep = PinnedRetrievalHit(_SNAPSHOT, "m-pair", 1.0)
    shared = fuse(
        (*filler, deep), (PinnedRetrievalHit(_SNAPSHOT, "m-solo", 1.0), *filler[:48], deep)
    )
    depths = {hit.member_id: hit for hit in shared}
    assert (depths["m-pair"].vector_rank, depths["m-pair"].lexical_rank) == (50, 50)
    assert depths["m-pair"].fused_score > depths["m-solo"].fused_score


def _ranking(marked: str, at_rank: int, prefix: str) -> tuple[PinnedRetrievalHit, ...]:
    """A full-depth ranking whose ``at_rank``-th seat is ``marked``; the rest are filler."""
    return tuple(
        PinnedRetrievalHit(
            _SNAPSHOT, marked if rank == at_rank else f"{prefix}{rank:02d}", 1.0 / rank
        )
        for rank in range(1, _CHANNEL_LIMIT + 1)
    )


def test_a_routed_member_never_outranks_a_member_a_scoring_channel_reached() -> None:
    """Exhaustively over the rank grid: no page the router chose can take a seat from a
    member BM25 or the vector channel scored, however deep that member sits.

    This is the property ``tree_k`` exists for, and it is measured, not cosmetic. Fused as a
    peer at ``k``, a tree rank-1 member scored 1/(60 + 1) = 0.01639 and beat a member only
    BM25 reached at rank 2, 1/(60 + 2) = 0.01613 — and a chart only one channel can score is
    precisely what ADR 0012's guaranteed seat exists for. The frozen gold set fell from
    21/22 to 17/22 passing, losing ``chart_value p7`` and ``chart_value p17`` outright.
    Now the best a routed member can earn is 1/(600 + 1) = 0.00166 and the least a scored
    one earns inside the channel limit is 1/(60 + 50) = 0.00909 — a 5.5x margin.
    """
    for real_rank in range(1, _CHANNEL_LIMIT + 1):
        scored = _ranking("m-scored", real_rank, "m-fill-")
        for tree_rank in range(1, _CHANNEL_LIMIT + 1):
            tree = _ranking("m-routed", tree_rank, "m-page-")
            for vector, lexical in ((scored, ()), ((), scored)):
                fused = fuse(vector, lexical, tree)
                by_id = {hit.member_id: hit for hit in fused}
                assert by_id["m-scored"].fused_score > by_id["m-routed"].fused_score, (
                    real_rank,
                    tree_rank,
                )
                order = [hit.member_id for hit in fused]
                assert order.index("m-scored") < order.index("m-routed")


def test_a_routed_member_still_outranks_one_no_scoring_channel_ranked_at_all() -> None:
    """Demoting the tree is not switching it off: a member no channel could score still
    enters the ranking, underneath them, where nothing stood before."""
    vector = (PinnedRetrievalHit(_SNAPSHOT, "m-scored", 0.9),)
    tree = (PinnedRetrievalHit(_SNAPSHOT, "m-routed", 1.0),)
    assert [hit.member_id for hit in fuse(vector, ())] == ["m-scored"]
    fused = fuse(vector, (), tree)
    assert [hit.member_id for hit in fused] == ["m-scored", "m-routed"]
    by_id = {hit.member_id: hit for hit in fused}
    assert by_id["m-routed"].fused_score == 1.0 / 601.0 > 0.0
    # A member nothing ranked is not a low-scoring hit; it is simply not in the fusion.
    assert "m-unrouted" not in by_id


def test_a_tree_ranking_leaves_the_two_scoring_channels_exactly_where_they_were() -> None:
    """Routing members in never reorders or rescores the members the channels found."""
    vector = tuple(
        PinnedRetrievalHit(_SNAPSHOT, f"m-v{rank:02d}", 1.0 / rank) for rank in range(1, 6)
    )
    lexical = tuple(PinnedRetrievalHit(_SNAPSHOT, f"m-v{rank:02d}", 1.0) for rank in (3, 1, 5))
    without = fuse(vector, lexical)
    with_tree = fuse(
        vector,
        lexical,
        tuple(PinnedRetrievalHit(_SNAPSHOT, f"m-t{rank:02d}", 1.0) for rank in range(1, 4)),
    )
    assert [hit.member_id for hit in with_tree][: len(without)] == [
        hit.member_id for hit in without
    ]
    assert [hit.fused_score for hit in with_tree][: len(without)] == [
        hit.fused_score for hit in without
    ]
    assert [hit.member_id for hit in with_tree][len(without) :] == ["m-t01", "m-t02", "m-t03"]


def test_the_tree_constant_must_keep_a_routed_page_below_every_scored_seat() -> None:
    """``tree_rrf_k + 1 > rrf_k + channel_limit``: the best a routed member can earn,
    1/(tree_rrf_k + 1), must stay strictly under the least a member one scoring channel
    ranked inside the limit earns, 1/(rrf_k + channel_limit). The defaults leave
    1/601 = 0.00166 against 1/110 = 0.00909, a 5.5x margin."""
    document = _FakeDocument(())
    HybridSearch(document, channel_limit=_CHANNEL_LIMIT)  # 601 > 110: the defaults satisfy it
    HybridSearch(document, channel_limit=_CHANNEL_LIMIT, rrf_k=60.0, tree_rrf_k=110.0)  # 111 > 110
    with pytest.raises(ValueError, match=r"tree_rrf_k \+ 1 > rrf_k \+ channel_limit"):
        # 110 == 110: a routed rank-1 member would tie the weakest scored seat, not lose to it.
        HybridSearch(document, channel_limit=_CHANNEL_LIMIT, rrf_k=60.0, tree_rrf_k=109.0)
    with pytest.raises(ValueError, match=r"tree_rrf_k \+ 1 > rrf_k \+ channel_limit"):
        HybridSearch(document, channel_limit=_CHANNEL_LIMIT, rrf_k=60.0, tree_rrf_k=60.0)
    with pytest.raises(ValueError, match="positive"):
        HybridSearch(document, tree_rrf_k=0.0)


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


_FOREIGN_QUESTION = "代理人科技投入的三个阶段"  # a question outside the index's language
_RESTATED = "expense ratio"


def test_a_lexical_query_feeds_bm25_while_the_vector_channel_keeps_the_question() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    outcome = search.search(_FOREIGN_QUESTION, top_k=3, mode="rrf", lexical_query=_RESTATED)
    # The embedder is handed the question as asked; BM25 scores the restatement.
    assert document.search_calls == [(_FOREIGN_QUESTION, 3)]
    assert outcome == search.search(_RESTATED, top_k=3, mode="rrf")
    assert [hit.member_id for hit in outcome.hits if hit.lexical_rank is not None] == [
        hit.member_id for hit in lexical_rank(search.index, _RESTATED, limit=3)
    ]


def test_the_classifier_reads_the_lexical_query_not_the_question() -> None:
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    # On its own the question scores nothing lexically and takes the vector channel alone.
    assert search.search(_FOREIGN_QUESTION, top_k=3).mode == "vector_only"
    assert document.search_calls == [(_FOREIGN_QUESTION, 3)]

    outcome = search.search(_FOREIGN_QUESTION, top_k=3, lexical_query=_RESTATED)
    assert outcome.mode == "bm25_only"
    assert document.search_calls == [(_FOREIGN_QUESTION, 3)]  # routed away from the embedder


@pytest.mark.parametrize("mode", ("auto", "rrf", "bm25_only", "vector_only"))
def test_an_omitted_lexical_query_leaves_every_mode_scoring_the_question(mode: FusionMode) -> None:
    question = "Which channels drove the change in the expense ratio and why did it move?"
    plain = _FakeDocument(("m-c", "m-a", "m-b"))
    echoed = _FakeDocument(("m-c", "m-a", "m-b"))
    outcome = HybridSearch(plain, channel_limit=3).search(question, top_k=3, mode=mode)
    restated = HybridSearch(echoed, channel_limit=3).search(
        question, top_k=3, mode=mode, lexical_query=question
    )
    assert outcome == restated
    assert plain.search_calls == echoed.search_calls


def test_the_rerank_judge_is_shown_the_question_the_user_asked() -> None:
    document = FakeDocument(
        tuple(FakeMember(member_id, text) for member_id, text in _TEXTS.items()), ("m-a", "m-b")
    )
    judge = _CountingJudge()
    HybridSearch(document, reranker=judge).search(
        _FOREIGN_QUESTION, top_k=3, mode="rrf", lexical_query=_RESTATED
    )
    # The judge ranks candidates against what the user asked, not against the restatement.
    assert judge.queries == [_FOREIGN_QUESTION]


# Six members over three pages, laid out so reading order contradicts member-id order on
# every page: page 0 runs top to bottom, page 1 left to right, and a member with no
# rectangle sorts after every located one.
_PAGED_MEMBERS = (
    MemberText(
        "m-p0-alpha",
        ObjectKind.TEXT,
        0,
        "Group highlights for the first half",
        bbox=(10.0, 90.0, 90.0, 110.0),
    ),
    MemberText(
        "m-p0-zeta",
        ObjectKind.TEXT,
        0,
        "Opening summary of the interim results",
        bbox=(10.0, 10.0, 90.0, 30.0),
    ),
    MemberText(
        "m-p1-alpha",
        ObjectKind.TEXT,
        1,
        "Partnership channel commentary",
        bbox=(60.0, 50.0, 90.0, 70.0),
    ),
    MemberText("m-p1-void", ObjectKind.TEXT, 1, "Footnote without a rectangle"),
    MemberText(
        "m-p1-zulu", ObjectKind.TEXT, 1, "Agency channel commentary", bbox=(10.0, 50.0, 40.0, 70.0)
    ),
    MemberText(
        "m-p2-solo",
        ObjectKind.TEXT,
        2,
        "Reconciliation table appendix",
        bbox=(10.0, 10.0, 90.0, 30.0),
    ),
)


def test_tree_pages_rank_their_members_page_by_page_in_reading_order() -> None:
    """The router's page order decides between pages; ``reading_key`` decides within one."""
    document = _FakeDocument((), members=_PAGED_MEMBERS)
    search = HybridSearch(document, channel_limit=10)
    outcome = search.search("q", top_k=10, mode="vector_only", tree_pages=(2, 0, 1))
    assert [hit.member_id for hit in outcome.hits] == [
        "m-p2-solo",
        "m-p0-zeta",
        "m-p0-alpha",
        "m-p1-zulu",
        "m-p1-alpha",
        "m-p1-void",
    ]
    assert [hit.tree_rank for hit in outcome.hits] == [1, 2, 3, 4, 5, 6]
    assert all(hit.vector_rank is None and hit.lexical_rank is None for hit in outcome.hits)
    assert all(hit.vector_score is None and hit.bm25_score is None for hit in outcome.hits)
    # A page the router did not choose contributes nothing, whatever it contains.
    assert (
        search.search("q", top_k=10, mode="vector_only", tree_pages=(2,)).hits == outcome.hits[:1]
    )


def test_tree_pages_promote_a_member_neither_other_channel_ranks() -> None:
    document = _FakeDocument(("m-p0-alpha",), members=_PAGED_MEMBERS)
    search = HybridSearch(document, channel_limit=10)
    plain = search.search("agency channel", top_k=5, mode="rrf").hits
    assert all(hit.member_id != "m-p2-solo" for hit in plain)

    routed = search.search("agency channel", top_k=5, mode="rrf", tree_pages=(2,)).hits
    promoted = next(hit for hit in routed if hit.member_id == "m-p2-solo")
    assert (promoted.tree_rank, promoted.vector_rank, promoted.lexical_rank) == (1, None, None)
    # The members the other two channels already found keep their ranks and their order.
    assert [hit.member_id for hit in routed if hit.member_id != "m-p2-solo"] == [
        hit.member_id for hit in plain
    ]


@pytest.mark.parametrize("mode", ("auto", "rrf", "bm25_only", "vector_only"))
def test_tree_pages_join_whatever_channel_mode_the_query_resolves_to(mode: FusionMode) -> None:
    """The caller decides whether to route; the resolved mode never vetoes the pages it chose."""
    document = _FakeDocument(("m-p0-alpha",), members=_PAGED_MEMBERS)
    search = HybridSearch(document, channel_limit=10)
    outcome = search.search("agency channel", top_k=6, mode=mode, tree_pages=(2,))
    routed = next(hit for hit in outcome.hits if hit.member_id == "m-p2-solo")
    assert routed.tree_rank == 1


def test_tree_pages_honour_the_member_narrowing_and_the_channel_limit() -> None:
    document = _FakeDocument((), members=_PAGED_MEMBERS)
    narrowed = HybridSearch(document, channel_limit=10).search(
        "q",
        top_k=10,
        mode="vector_only",
        allowed=frozenset({"m-p1-alpha", "m-p2-solo"}),
        tree_pages=(1, 2),
    )
    assert [hit.member_id for hit in narrowed.hits] == ["m-p1-alpha", "m-p2-solo"]
    assert [hit.tree_rank for hit in narrowed.hits] == [1, 2]

    capped = HybridSearch(document, channel_limit=2).search(
        "q", top_k=10, mode="vector_only", tree_pages=(1, 0)
    )
    # The cap is spent in the router's order: page 1's first two members, and nothing after.
    assert [hit.member_id for hit in capped.hits] == ["m-p1-zulu", "m-p1-alpha"]


def test_search_without_tree_pages_is_pinned_to_the_two_channel_fusion() -> None:
    """Every rank, score and seat of a routine request, spelled out: adding the third
    channel must leave an unrouted question byte for byte where it was."""
    document = _FakeDocument(("m-c", "m-a", "m-b"))
    search = HybridSearch(document, channel_limit=3)
    question = "Which channels drove the change in the expense ratio and why did it move?"
    bm25 = {hit.member_id: hit.score for hit in lexical_rank(search.index, question, limit=3)}
    fused = rrf_fuse([["m-c", "m-a", "m-b"], ["m-a", "m-b"]], 60)
    expected = (
        FusedHit(_SNAPSHOT, "m-a", fused["m-a"], 2, 1, 0.9, bm25["m-a"], None),
        FusedHit(_SNAPSHOT, "m-b", fused["m-b"], 3, 2, 0.8, bm25["m-b"], None),
        FusedHit(_SNAPSHOT, "m-c", fused["m-c"], 1, None, 1.0, None, None),
    )
    assert search.search(question, top_k=3, mode="rrf").hits == expected
    assert search.search(question, top_k=3, mode="rrf", tree_pages=()).hits == expected
