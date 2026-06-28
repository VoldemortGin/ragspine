"""W11 ColBERT 晚交互单测：MaxSim 打分 + ColBERTReranker（ListwiseJudge）+ make_reranker + 隔离继承。

全部用 FakeMultiVectorBackend 替身（零网络零模型）。隔离：经 listwise_rerank 时 RESTRICTED 文本绝不
进入 backend.embed_documents（继承自编排出口）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import RetrievalResult
from ragspine.retrieval.representation.late_interaction import (
    ColBERTReranker,
    FastEmbedColBERTBackend,
    MultiVectorBackend,
    max_sim,
)
from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY, listwise_rerank


class FakeMultiVectorBackend:
    """按 text -> token 向量表的替身，记录被嵌入的文档（供隔离断言）。"""

    def __init__(self, query_vecs, doc_map):
        self.query_vecs = query_vecs
        self.doc_map = doc_map
        self.embedded_docs: list[str] = []

    def embed_query(self, query):
        return self.query_vecs

    def embed_documents(self, docs):
        self.embedded_docs.extend(docs)
        return [self.doc_map[d] for d in docs]


def test_max_sim_basic():
    # query 两 token，doc 两 token；每个 query token 取对 doc 的最大 cosine 再求和。
    q = [[1.0, 0.0], [0.0, 1.0]]
    d = [[1.0, 0.0], [0.0, 1.0]]
    assert max_sim(q, d) == pytest.approx(2.0)
    assert max_sim([], d) == 0.0
    assert max_sim(q, []) == 0.0


def test_max_sim_partial():
    q = [[1.0, 0.0]]
    d = [[1.0, 0.0], [0.5, 0.5]]  # 第一 doc token cosine=1.0 最大
    assert max_sim(q, d) == pytest.approx(1.0)


def test_colbert_reranker_ranks_by_maxsim():
    """doc A 与 query 完全对齐（高分），doc B 正交（低分）-> A 排前。"""
    backend = FakeMultiVectorBackend(
        query_vecs=[[1.0, 0.0]],
        doc_map={
            "docA": [[1.0, 0.0]],   # MaxSim = 1.0
            "docB": [[0.0, 1.0]],   # MaxSim = 0.0
        },
    )
    order = ColBERTReranker(backend).judge("q", ["docB", "docA"])
    assert order == [1, 0], "docA（下标1）应排第一"


def test_colbert_empty_candidates_no_load():
    backend = FakeMultiVectorBackend([[1.0]], {})
    assert ColBERTReranker(backend).judge("q", []) == []
    assert backend.embedded_docs == []


def test_colbert_deterministic_and_stable_tie():
    backend = FakeMultiVectorBackend(
        query_vecs=[[1.0, 0.0]],
        doc_map={"a": [[1.0, 0.0]], "b": [[1.0, 0.0]], "c": [[0.0, 1.0]]},
    )
    r1 = ColBERTReranker(backend).judge("q", ["a", "b", "c"])
    r2 = ColBERTReranker(backend).judge("q", ["a", "b", "c"])
    assert r1 == r2
    # a、b 平分 -> 保持原序 a 在 b 前；c 最低在末。
    assert r1 == [0, 1, 2]


def test_make_reranker_resolves_colbert():
    """make_reranker('colbert') 返回 ColBERTReranker（不触发 fastembed 加载——构造极轻）。"""
    assert isinstance(make_reranker("colbert"), ColBERTReranker)
    assert isinstance(make_reranker("late_interaction"), ColBERTReranker)


def test_make_reranker_colbert_model_env(monkeypatch):
    monkeypatch.setenv("RAGSPINE_COLBERT_MODEL", "custom/colbert")
    r = make_reranker("colbert")
    assert isinstance(r, ColBERTReranker)
    assert r.model_name == "custom/colbert"


def test_fastembed_backend_constructs_without_fastembed():
    """适配器构造极轻：不 import fastembed、不加载模型（没装 [colbert] 也能构造 / auto 探测）。"""
    b = FastEmbedColBERTBackend()
    assert b.model_name
    assert b._model is None


# --- 隔离继承：经 listwise_rerank 时 RESTRICTED 文本绝不进 backend ---

SECRET = "SECRET-EXEC-PR 高管评级（RESTRICTED 不出域）"


def _result(i, text, sensitivity="INTERNAL"):
    chunk = Chunk(
        chunk_id=f"d{i}#c0", doc_id=f"d{i}", seq=0, text=text,
        source_locator=f"d{i}#para1", para_start=1, para_end=1, sensitivity=sensitivity,
    )
    return RetrievalResult(chunk=chunk, bm25_score=1.0, vector_score=0.0, fused_score=1.0 / (i + 1))


def test_colbert_never_embeds_restricted_via_listwise():
    backend = FakeMultiVectorBackend(
        query_vecs=[[1.0, 0.0]],
        doc_map={"公开甲": [[1.0, 0.0]], "公开乙": [[0.5, 0.5]]},
    )
    results = [
        _result(0, "公开甲"),
        _result(1, SECRET, sensitivity=RESTRICTED_SENSITIVITY),
        _result(2, "公开乙"),
    ]
    out = listwise_rerank("q", results, ColBERTReranker(backend))
    assert SECRET not in backend.embedded_docs
    assert set(backend.embedded_docs) == {"公开甲", "公开乙"}
    # RESTRICTED 块原位保留。
    assert out[1].chunk.text == SECRET


def test_runtime_checkable_protocol():
    backend = FakeMultiVectorBackend([[1.0]], {})
    assert isinstance(backend, MultiVectorBackend)
