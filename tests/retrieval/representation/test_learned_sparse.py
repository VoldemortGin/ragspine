"""W11 SPLADE 学习稀疏单测：sparse_dot + SpladeReranker（ListwiseJudge）+ make_reranker + 隔离继承。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import RetrievalResult
from ragspine.retrieval.representation.learned_sparse import (
    FastEmbedSpladeBackend,
    SparseEmbeddingBackend,
    SpladeReranker,
    sparse_dot,
)
from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY, listwise_rerank


class FakeSparseBackend:
    def __init__(self, query_vec, doc_map):
        self.query_vec = query_vec
        self.doc_map = doc_map
        self.embedded_docs: list[str] = []

    def embed_query(self, query):
        return self.query_vec

    def embed_documents(self, docs):
        self.embedded_docs.extend(docs)
        return [self.doc_map[d] for d in docs]


def test_sparse_dot_basic():
    assert sparse_dot({1: 2.0, 3: 1.0}, {1: 0.5, 3: 4.0}) == 2.0 * 0.5 + 1.0 * 4.0
    assert sparse_dot({}, {1: 1.0}) == 0.0
    assert sparse_dot({1: 1.0}, {2: 1.0}) == 0.0  # 无共同维


def test_splade_reranker_ranks_by_sparse_dot():
    backend = FakeSparseBackend(
        query_vec={1: 1.0, 2: 1.0},
        doc_map={
            "high": {1: 2.0, 2: 2.0},   # dot = 4.0
            "low": {1: 0.1},            # dot = 0.1
        },
    )
    order = SpladeReranker(backend).judge("q", ["low", "high"])
    assert order == [1, 0]


def test_splade_empty_candidates_no_load():
    backend = FakeSparseBackend({1: 1.0}, {})
    assert SpladeReranker(backend).judge("q", []) == []
    assert backend.embedded_docs == []


def test_splade_deterministic():
    backend = FakeSparseBackend(
        {1: 1.0}, {"a": {1: 1.0}, "b": {1: 1.0}, "c": {2: 1.0}}
    )
    assert SpladeReranker(backend).judge("q", ["a", "b", "c"]) == SpladeReranker(backend).judge(
        "q", ["a", "b", "c"]
    )


def test_make_reranker_resolves_splade():
    assert isinstance(make_reranker("splade"), SpladeReranker)
    assert isinstance(make_reranker("learned_sparse"), SpladeReranker)


def test_make_reranker_splade_model_env(monkeypatch):
    monkeypatch.setenv("RAGSPINE_SPLADE_MODEL", "custom/splade")
    r = make_reranker("splade")
    assert isinstance(r, SpladeReranker)
    assert r.model_name == "custom/splade"


def test_fastembed_splade_backend_constructs_without_fastembed():
    b = FastEmbedSpladeBackend()
    assert b.model_name
    assert b._model is None


SECRET = "SECRET-EXEC-PR 高管评级（RESTRICTED 不出域）"


def _result(i, text, sensitivity="INTERNAL"):
    chunk = Chunk(
        chunk_id=f"d{i}#c0", doc_id=f"d{i}", seq=0, text=text,
        source_locator=f"d{i}#para1", para_start=1, para_end=1, sensitivity=sensitivity,
    )
    return RetrievalResult(chunk=chunk, bm25_score=1.0, vector_score=0.0, fused_score=1.0 / (i + 1))


def test_splade_never_embeds_restricted_via_listwise():
    backend = FakeSparseBackend(
        {1: 1.0}, {"公开甲": {1: 2.0}, "公开乙": {1: 0.5}}
    )
    results = [
        _result(0, "公开甲"),
        _result(1, SECRET, sensitivity=RESTRICTED_SENSITIVITY),
        _result(2, "公开乙"),
    ]
    out = listwise_rerank("q", results, SpladeReranker(backend))
    assert SECRET not in backend.embedded_docs
    assert set(backend.embedded_docs) == {"公开甲", "公开乙"}
    assert out[1].chunk.text == SECRET


def test_runtime_checkable_protocol():
    assert isinstance(FakeSparseBackend({}, {}), SparseEmbeddingBackend)
