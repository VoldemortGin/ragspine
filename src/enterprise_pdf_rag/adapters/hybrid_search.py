"""Hybrid retrieval over one pinned snapshot: vector channel + BM25 + RRF.

Only the three pure ranking functions and the listwise rerank orchestration are
borrowed from ``ragspine``; the corpus is each member's embedded index text
(``member_texts``), so both channels score exactly the same text. Rerank is
opt-in: with no judge injected, no model is consulted; when it runs, the judge
reads each candidate's resolved evidence block, not the index text.

Which channels a query actually uses is a decision, not a constant: ``answers/query_mode``
routes a short label-and-period query to BM25 alone, where the measured recall is higher
(ADR 0018). A single-channel mode is expressed as a fusion with one empty ranking, so every
mode shares one scoring path.

A third ranking joins the fusion when the caller supplies ``tree_pages`` (ADR 0019): the
0-based page indices an LLM chose by reasoning over the document's table-of-contents tree,
whose members enter the ranking page by page in reading order. It is a page set rather than
a similarity, so it contributes a rank and no score of its own, and it participates whatever
channel mode the query resolves to — supplying no pages leaves every existing ranking
untouched. Because that rank is reading order and not relevance, it is fused with its own,
much larger RRF constant (``tree_k``): a member **only** the tree reached sorts below every
member a scoring channel reached, so the tree adds recall at the tail instead of competing
for the head. The term is still additive, so it can reorder two scored members between
themselves; what it cannot do is put an unscored page above a scored one.
"""

from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable

from enterprise_pdf_rag.answers.models import FusedHit as FusedHit
from enterprise_pdf_rag.answers.page_window import reading_key
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.answers.query_mode import FusionMode, QueryMode, classify_query
from enterprise_pdf_rag.processing.context_builder import build_context_block
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.common.evidence.providers.local_models import RerankResult
from ragspine.retrieval.lexical.retrieval import bm25_scores, rrf_fuse, tokenize
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge, listwise_rerank

_TOKENIZER_TAG = "ragspine-lexical-tokenize-v1"


def lexical_index_id(snapshot_id: str, *, k1: float, b: float) -> str:
    """Content address of a lexical index: the snapshot plus every scoring parameter."""
    return sha256(
        repr(("lexical-bm25-v1", snapshot_id, k1, b, _TOKENIZER_TAG)).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class LexicalIndex:
    snapshot_id: str
    member_ids: tuple[str, ...]
    docs_tokens: tuple[tuple[str, ...], ...]
    k1: float = 1.5
    b: float = 0.75
    # The corpus units themselves (same order as ``member_ids``), so metadata filters and
    # seat selection read them from the cache instead of a second ``member_texts()`` call.
    members: tuple[MemberText, ...] = ()

    def __post_init__(self) -> None:
        if len(self.member_ids) != len(self.docs_tokens):
            raise ValueError("Lexical index members and token lists must align")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("Lexical index members must be unique")
        if self.members and tuple(item.member_id for item in self.members) != self.member_ids:
            raise ValueError("Lexical index members must align with their texts")

    @property
    def index_id(self) -> str:
        return lexical_index_id(self.snapshot_id, k1=self.k1, b=self.b)


def build_lexical_index(
    document: MountedDocument, *, k1: float = 1.5, b: float = 0.75
) -> LexicalIndex:
    """Tokenize every member's index text; empty members stay in the corpus at score 0."""
    members = sorted(document.member_texts(), key=lambda item: item.member_id)
    return LexicalIndex(
        document.retrieval_snapshot_id,
        tuple(member.member_id for member in members),
        tuple(tuple(tokenize(member.text)) for member in members),
        k1,
        b,
        tuple(members),
    )


def lexical_rank(
    index: LexicalIndex,
    query: str,
    *,
    limit: int,
    allowed: frozenset[str] | None = None,
) -> tuple[PinnedRetrievalHit, ...]:
    """BM25 ranking; zero scores are dropped and ties break on member id.

    ``allowed`` keeps only those members (a metadata pre-filter); corpus statistics
    are those of the whole snapshot either way.
    """
    if not query.strip():
        raise ValueError("A nonempty query is required")
    if limit < 1:
        raise ValueError("Lexical limit must be at least one")
    scores = bm25_scores(
        tokenize(query), [list(tokens) for tokens in index.docs_tokens], k1=index.k1, b=index.b
    )
    hits = [
        PinnedRetrievalHit(index.snapshot_id, member_id, score)
        for member_id, score in zip(index.member_ids, scores, strict=True)
        if score > 0 and (allowed is None or member_id in allowed)
    ]
    return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.member_id))[:limit])


def fuse(
    vector: Sequence[PinnedRetrievalHit],
    lexical: Sequence[PinnedRetrievalHit],
    tree: Sequence[PinnedRetrievalHit] = (),
    *,
    k: float = 60.0,
    tree_k: float = 600.0,
) -> tuple[FusedHit, ...]:
    """Reciprocal rank fusion of the channel rankings pinned to the same snapshot.

    The two scoring channels share ``k``; the ADR 0019 ``tree`` ranking is fused with its
    own, much larger ``tree_k``, because its rank is a page set read in reading order and
    not a relevance it ever measured. ``tree`` defaults to empty, which fuses exactly as
    the two-channel call always did.
    """
    snapshots = {hit.snapshot_id for hit in (*vector, *lexical, *tree)}
    if len(snapshots) > 1:
        raise ValueError("Hybrid channels belong to different retrieval snapshots")
    if not snapshots:
        return ()
    (snapshot_id,) = snapshots
    vector_ranks = {hit.member_id: (rank, hit.score) for rank, hit in enumerate(vector, start=1)}
    lexical_ranks = {hit.member_id: (rank, hit.score) for rank, hit in enumerate(lexical, start=1)}
    tree_ranks = {hit.member_id: rank for rank, hit in enumerate(tree, start=1)}
    # RRF ranks are 1-based, so with ``k`` at 60 and rankings cut to fifty a member only one
    # scoring channel ranks scores at most 1/(60 + 1) = 0.0164, while a member both scoring
    # channels rank scores at least 2/(60 + 50) = 0.0182: agreement beats depth. That margin
    # is what ``rrf_k`` buys — do not change it.
    fused = rrf_fuse([list(vector_ranks), list(lexical_ranks)], k)
    # The tree is not a peer of those two. Its rank orders the routed pages in reading order,
    # which is not a relevance, so it is fused at ``tree_k`` instead of ``k``: the best a
    # routed member can earn is 1/(tree_k + 1), and the least a member one scoring channel
    # ranks inside the channel limit earns is 1/(k + channel_limit). At the constants the
    # service runs (``tree_k`` 600, ``k`` 60, a channel limit of 50) that reads
    # 1/601 = 0.00166 against 1/110 = 0.00909 — a 5.5x margin — so a member only the tree
    # reached lands below every scored seat. The term is additive, so a member a channel
    # did score can still move relative to another scored member; the guarantee is about
    # unscored pages only. ``HybridSearch`` keeps ``tree_k + 1 > k + channel_limit`` true.
    for member_id, rank in tree_ranks.items():
        fused[member_id] = fused.get(member_id, 0.0) + 1.0 / (tree_k + rank)
    hits = [
        FusedHit(
            snapshot_id,
            member_id,
            score,
            vector_ranks[member_id][0] if member_id in vector_ranks else None,
            lexical_ranks[member_id][0] if member_id in lexical_ranks else None,
            vector_ranks[member_id][1] if member_id in vector_ranks else None,
            lexical_ranks[member_id][1] if member_id in lexical_ranks else None,
            tree_ranks.get(member_id),
        )
        for member_id, score in fused.items()
    ]
    return tuple(sorted(hits, key=lambda hit: (-hit.fused_score, hit.member_id)))


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """One ranking plus the channel decision that produced it."""

    mode: QueryMode
    hits: tuple[FusedHit, ...]


@dataclass(frozen=True, slots=True)
class _Chunk:
    text: str
    sensitivity: str = "INTERNAL"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """The duck shape ``listwise_rerank`` expects: ``.chunk.text`` and ``.chunk.sensitivity``."""

    hit: FusedHit
    chunk: _Chunk


class HybridSearch:
    def __init__(
        self,
        document: MountedDocument,
        *,
        channel_limit: int = 20,
        rrf_k: float = 60.0,
        tree_rrf_k: float = 600.0,
        reranker: ListwiseJudge | None = None,
        index_cache: MutableMapping[str, LexicalIndex] | None = None,
    ) -> None:
        if channel_limit < 1 or rrf_k <= 0 or tree_rrf_k <= 0:
            raise ValueError("Hybrid search requires a positive channel limit and RRF k")
        # The property the tree channel is allowed to have: a member only it reached sorts
        # below every member a scoring channel reached. (The term is additive, so two scored
        # members can still move relative to each other.) The best a routed
        # member can earn is 1/(tree_rrf_k + 1); the least a member one scoring channel ranked
        # inside the channel limit earns is 1/(rrf_k + channel_limit). The first must stay
        # strictly below the second, which is exactly ``tree_rrf_k + 1 > rrf_k + channel_limit``
        # — at the constants the service runs (``tree_rrf_k`` 600, ``rrf_k`` 60, and the
        # ``AnswerRequest.channel_limit`` of 50), 1/601 = 0.00166 against 1/110 = 0.00909:
        # a 5.5x margin.
        if tree_rrf_k + 1 <= rrf_k + channel_limit:
            raise ValueError(
                "The tree RRF k must keep a routed page below every scored seat: "
                f"tree_rrf_k + 1 > rrf_k + channel_limit, so that the best tree term "
                f"1/({tree_rrf_k} + 1) stays under the weakest scored term "
                f"1/({rrf_k} + {channel_limit})"
            )
        self._document = document
        self._channel_limit = channel_limit
        self._rrf_k = rrf_k
        self._tree_rrf_k = tree_rrf_k
        self._reranker = reranker
        cache = {} if index_cache is None else index_cache
        key = lexical_index_id(document.retrieval_snapshot_id, k1=1.5, b=0.75)
        index = cache.get(key)
        if index is None or index.snapshot_id != document.retrieval_snapshot_id:
            index = build_lexical_index(document)
            cache[key] = index
        self._index = index

    @property
    def index(self) -> LexicalIndex:
        return self._index

    def lexical_hits(self, query: str, *, allowed: frozenset[str] | None = None) -> int:
        """How many members BM25 can score for this query; zero means the query shares no
        vocabulary with the index, which is what a foreign-language question looks like."""
        return len(lexical_rank(self._index, query, limit=self._channel_limit, allowed=allowed))

    def _vector_rank(
        self, query: str, allowed: frozenset[str] | None
    ) -> tuple[PinnedRetrievalHit, ...]:
        if allowed is None:
            return self._document.search(query, limit=self._channel_limit)
        return tuple(
            hit
            for hit in self._document.search(
                query, limit=max(self._channel_limit, len(self._index.member_ids))
            )
            if hit.member_id in allowed
        )[: self._channel_limit]

    def _tree_rank(
        self, tree_pages: Sequence[int], allowed: frozenset[str] | None
    ) -> tuple[PinnedRetrievalHit, ...]:
        """The members of the routed pages, page by page in the router's order (ADR 0019).

        Within a page the members are read in ``answers.page_window.reading_key`` order, the
        same order the page context block prints them in. The score is synthetic and strictly
        descending: the tree channel ranks pages, not similarities, so only the order is real.
        """
        if not tree_pages:
            return ()
        wanted = set(tree_pages)
        by_page: dict[int, list[MemberText]] = {page: [] for page in wanted}
        for member in self._index.members:
            if member.page_index in wanted:
                by_page[member.page_index].append(member)
        hits: list[PinnedRetrievalHit] = []
        seen: set[str] = set()
        for page in tree_pages:
            for member in sorted(by_page.get(page, ()), key=reading_key):
                if member.member_id in seen or (
                    allowed is not None and member.member_id not in allowed
                ):
                    continue
                if len(hits) >= self._channel_limit:
                    return tuple(hits)
                seen.add(member.member_id)
                hits.append(
                    PinnedRetrievalHit(
                        self._index.snapshot_id, member.member_id, 1.0 / (len(hits) + 1)
                    )
                )
        return tuple(hits)

    def search(
        self,
        query: str,
        *,
        top_k: int,
        allowed: frozenset[str] | None = None,
        mode: FusionMode = "auto",
        lexical_query: str | None = None,
        tree_pages: Sequence[int] = (),
    ) -> SearchOutcome:
        """Rank over the channels ``mode`` selects; ``auto`` classifies the query (ADR 0018).

        ``allowed`` narrows each channel to those members first. With a narrowing the
        vector channel is read over the whole corpus and cut to the channel limit after
        filtering, so the filter never starves it. ``bm25_only`` skips the vector channel
        altogether, which is one embedding call the request never makes.

        ``lexical_query`` is what BM25 and the channel classifier score when it differs from
        the question — a restatement in the index's language, which is the only thing a
        token-matching channel can score at all. The vector channel and the rerank judge keep
        ``query``: both read the question as language, so a restatement only trades the
        asker's wording for someone else's. It defaults to ``query``, which scores both
        channels on one string exactly as before.

        ``tree_pages`` are the 0-based page indices the ADR 0019 router chose, in the order
        it returned them after its ascending sort; their members join the fusion as a third
        ranking whatever ``QueryMode`` resolves, since it is the caller that decides whether
        to route at all, and are fused at ``tree_rrf_k`` so they add recall underneath the
        scored seats instead of taking them. Empty — the default — leaves every ranking
        exactly as it was.
        """
        if top_k < 1:
            raise ValueError("top_k must be at least one")
        scoreable = query if lexical_query is None else lexical_query
        lexical: tuple[PinnedRetrievalHit, ...] = (
            ()
            if mode == "vector_only"
            else lexical_rank(self._index, scoreable, limit=self._channel_limit, allowed=allowed)
        )
        resolved: QueryMode = (
            classify_query(scoreable, lexical_hits=len(lexical)) if mode == "auto" else mode
        )
        vector = () if resolved == "bm25_only" else self._vector_rank(query, allowed)
        # Fusing one ranking with an empty one *is* that ranking, scored the same way, so a
        # single-channel mode needs no second ranking path.
        fused = fuse(
            vector,
            () if resolved == "vector_only" else lexical,
            self._tree_rank(tree_pages, allowed),
            k=self._rrf_k,
            tree_k=self._tree_rrf_k,
        )
        if self._reranker is None or not fused:
            return SearchOutcome(resolved, fused[:top_k])
        # The judge sees what the answer model would see: a chart candidate's citable
        # ``points.<id>.value`` lines, a text candidate's spans. Bounded by the fused
        # set (at most twice the channel limit); every candidate is a verified resolve.
        candidates = [
            _Candidate(
                hit,
                _Chunk(build_context_block(self._document.resolve(hit.as_hit())).prompt_text()),
            )
            for hit in fused
        ]
        reranked = listwise_rerank(query, candidates, self._reranker, top_n=top_k)
        return SearchOutcome(resolved, tuple(candidate.hit for candidate in reranked))


@runtime_checkable
class RerankPort(Protocol):
    def rerank(
        self, query: str, documents: tuple[str, ...], *, limit: int
    ) -> tuple[RerankResult, ...]: ...


class LocalRerankJudge:
    """Adapt the local rerank endpoint to ragspine's listwise judge protocol."""

    def __init__(self, adapter: RerankPort) -> None:
        self._adapter = adapter

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        if not candidates:
            return []
        results = self._adapter.rerank(query, tuple(candidates), limit=len(candidates))
        return [result.index for result in results]
