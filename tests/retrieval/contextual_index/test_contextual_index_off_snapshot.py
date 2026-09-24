"""标题进索引开关为 off（RAGSPINE_CONTEXTUAL_INDEX=off）时，叙事检索输出与向量库签名逐字节不变。

冻结摘要取自引入开关之前的实现（HEAD 8fc985d）：同一带标题语料、同一组 query，三种页级父子模式 ×
（纯 BM25 / 确定性向量 + 倒序 judge 精排）下 snippet 的 JSON 摘要；以及持久化向量库的 doc 签名。
默认构造、显式 index_text_fn=make_index_text_fn("off")、build_narrative_retriever(contextual_index="off")
必须命中同一摘要。
"""

import hashlib
import json
import os
import sqlite3

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.lexical.retrieval import NarrativeIndex, _record_metadata
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.store import VectorRecord

from .conftest import heading_corpus

_QUERIES = ("distribution mix", "Singapore VONB", "Falcon acquisition", "agency partnerships")

_FROZEN_RETRIEVAL = "51fb28bc9d9444c4d154f81394a43ed31b7db5128a704e4f643c27f588b8ee4f"
_FROZEN_SIGNATURES = "28c4295c7157e4a28dd6bba5741e10cfe9ea202af3af733ce4ca17f20a4cbef1"


class _ReverseJudge:
    def judge(self, query: str, candidates: list[str]) -> list[int]:
        return list(reversed(range(len(candidates))))


def _digest(store, make_index) -> str:
    dumps = []
    for page_parent in ("off", "dedup", "page+child"):
        for hybrid in (False, True):
            index = make_index(
                store,
                embedding_backend=DeterministicEmbeddingBackend() if hybrid else None,
                judge=_ReverseJudge() if hybrid else None,
                page_parent=page_parent,
            )
            if hybrid:
                chunks = [c for cs in heading_corpus().values() for c in cs]
                vectors = index.embedding_backend.embed_texts([c.text for c in chunks])
                index.vector_store.upsert(
                    [
                        VectorRecord(id=c.chunk_id, vector=tuple(v), metadata=_record_metadata(c))
                        for c, v in zip(chunks, vectors, strict=True)
                    ]
                )
            retriever = NarrativeIndexRetriever(index)
            for query in _QUERIES:
                for top_k in (2, 50):
                    dumps.append(retriever.retrieve(query, top_k=top_k))
    payload = json.dumps(dumps, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_default_matches_frozen_snapshot(heading_store):
    assert _digest(heading_store, NarrativeIndex) == _FROZEN_RETRIEVAL


def test_explicit_off_matches_frozen_snapshot(heading_store):
    from ragspine.retrieval.contextual import make_index_text_fn

    def make(store, **kw):
        return NarrativeIndex(store, index_text_fn=make_index_text_fn("off"), **kw)

    assert _digest(heading_store, make) == _FROZEN_RETRIEVAL


def test_build_narrative_retriever_off_matches_frozen_snapshot(heading_store, tmp_path):
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever

    db = tmp_path / "chunks.db"
    opened_stores = []

    def make(store, *, embedding_backend, judge, page_parent):
        retriever, opened = build_narrative_retriever(
            db,
            embedding_backend=embedding_backend,
            reranker=judge,
            page_parent=page_parent,
            contextual_index="off",
        )
        opened_stores.append(opened)
        return retriever.index

    try:
        assert _digest(heading_store, make) == _FROZEN_RETRIEVAL
    finally:
        for opened in opened_stores:
            opened.close()


def _signatures(vector_db) -> str:
    conn = sqlite3.connect(vector_db)
    try:
        rows = conn.execute("SELECT doc_id, signature FROM chunk_vector_docs ORDER BY doc_id")
        return hashlib.sha256(json.dumps(rows.fetchall()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def _sync(heading_store, vector_db, **kw):
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex

    index = ChunkVectorIndex(vector_db)
    try:
        return index.sync(
            heading_store.iter_chunks(), DeterministicEmbeddingBackend(), model_id="det", **kw
        )
    finally:
        index.close()


def test_vector_signatures_default_match_frozen(heading_store, tmp_path):
    pytest.importorskip("sqlite_vec")
    _sync(heading_store, tmp_path / "v.db")
    assert _signatures(tmp_path / "v.db") == _FROZEN_SIGNATURES


def test_vector_signatures_off_match_frozen(heading_store, tmp_path):
    pytest.importorskip("sqlite_vec")
    _sync(heading_store, tmp_path / "v.db", contextual_index="off")
    assert _signatures(tmp_path / "v.db") == _FROZEN_SIGNATURES
