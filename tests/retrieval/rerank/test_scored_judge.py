"""ScoredRerankJudge：把返回 (index, relevance_score) 的打分式重排器接成 ListwiseJudge。

替身 reranker 零网络；验证：按分降序、平分保原序、空候选不调用、空白候选不送打分且补尾、
与 listwise_rerank 编排衔接（RESTRICTED 仍不送 judge）。
"""

import os
from dataclasses import dataclass

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import RetrievalResult
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge, listwise_rerank
from ragspine.retrieval.rerank.scored_judge import ScoredRerankJudge


@dataclass(frozen=True)
class Scored:
    index: int
    relevance_score: float


class FakeReranker:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def rerank(self, query: str, documents: tuple[str, ...], *, limit: int) -> tuple[Scored, ...]:
        self.calls.append((query, documents, limit))
        ranked = sorted(
            (Scored(i, self.scores[d]) for i, d in enumerate(documents)),
            key=lambda s: -s.relevance_score,
        )
        return tuple(ranked[:limit])


def test_orders_by_score_descending() -> None:
    reranker = FakeReranker({"a": 0.1, "b": 0.9, "c": 0.5})
    judge = ScoredRerankJudge(reranker)
    assert judge.judge("q", ["a", "b", "c"]) == [1, 2, 0]
    assert reranker.calls == [("q", ("a", "b", "c"), 3)]


def test_ties_keep_original_order() -> None:
    judge = ScoredRerankJudge(FakeReranker({"a": 0.5, "b": 0.5, "c": 0.7}))
    assert judge.judge("q", ["a", "b", "c"]) == [2, 0, 1]


def test_empty_candidates_make_no_call() -> None:
    reranker = FakeReranker({})
    assert ScoredRerankJudge(reranker).judge("q", []) == []
    assert reranker.calls == []


def test_blank_candidates_are_not_scored_and_go_last() -> None:
    reranker = FakeReranker({"a": 0.1, "b": 0.9})
    judge = ScoredRerankJudge(reranker)
    assert judge.judge("q", ["a", "  ", "b"]) == [2, 0, 1]
    assert reranker.calls == [("q", ("a", "b"), 2)]


def test_satisfies_listwise_judge_protocol() -> None:
    assert isinstance(ScoredRerankJudge(FakeReranker({})), ListwiseJudge)


def test_restricted_text_never_reaches_the_reranker() -> None:
    def result(cid: str, text: str, sensitivity: str) -> RetrievalResult:
        chunk = Chunk(
            chunk_id=cid,
            doc_id="d",
            seq=0,
            text=text,
            source_locator="d@page=1#para1",
            para_start=1,
            para_end=1,
            sensitivity=sensitivity,
        )
        return RetrievalResult(chunk=chunk, bm25_score=1.0, vector_score=0.0, fused_score=1.0)

    reranker = FakeReranker({"open-a": 0.1, "open-b": 0.9})
    results = [
        result("c1", "open-a", "INTERNAL"),
        result("c2", "secret", "RESTRICTED"),
        result("c3", "open-b", "INTERNAL"),
    ]
    out = listwise_rerank("q", results, ScoredRerankJudge(reranker))
    assert [r.chunk.chunk_id for r in out] == ["c3", "c2", "c1"]
    assert all("secret" not in docs for _, docs, _ in reranker.calls)
