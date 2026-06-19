"""NarrativeIndex / build_narrative_retriever 接入 VectorStore 缝（接线 + 持久化）。

NarrativeIndex 持有一个 VectorStore（向量打分缝），入库即嵌入落盘；重入同 doc 走 doc 粒度
失效（delete where={doc_id}，非全清），守住「重入不串旧向量」的契约。build_narrative_retriever
/ ServiceConfig 把「选哪个 store」一路透传到服务边界。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.store import InProcessVectorStore


def _meta(doc_id: str, **overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id=doc_id, title=doc_id, topic="FIN", entity="ACME_HK",
        geography="HK", period="2025H1", language="zh", sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


@pytest.fixture
def store(tmp_db_path):
    s = ChunkStore(tmp_db_path)
    s.init_schema()
    yield s
    s.close()


def test_default_vector_store_is_in_process_with_backend(store):
    """有 embedding 后端、未注入 store：NarrativeIndex 自建零依赖内存默认。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    assert isinstance(index.vector_store, InProcessVectorStore)


def test_no_vector_store_when_pure_bm25(store):
    """无 embedding 后端：vector_store 保持 None（纯 BM25）。"""
    index = NarrativeIndex(store)
    assert index.vector_store is None


def test_vector_channel_active_through_index(store):
    """端到端：经 NarrativeIndex 入库+检索，向量通道真实出分（vector_score>0）。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    index.ingest("香港 REVENUE 营收持续增长", _meta("doc_fin"))
    results = index.retrieve("香港 REVENUE 营收", rerank=False)
    assert results
    assert results[0].vector_score > 0.0
    # 共享 store 被填充。
    assert index.vector_store.count() >= 1


def test_reingest_swaps_doc_vectors_no_stale(store):
    """同 doc 重入：旧版本向量【doc 粒度】撤下、新版本入库（无陈旧向量串味）。

    注：增量3 起入库即嵌入落盘 + doc 粒度失效（非 delete-all）；故重入后库内即为新版本向量。
    """
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    n1 = index.ingest("香港 REVENUE 营收下滑", _meta("doc_fin"))
    assert index.vector_store.count() == n1  # 入库即落盘
    # 同 doc 重入新内容 -> 旧版本向量撤下、新版本落盘（库内只剩新版本）。
    n2 = index.ingest("香港 PROFIT 利润大增 各项指标", _meta("doc_fin"))
    assert index.vector_store.count() == n2
    # 检索新内容，向量通道对【新文本】出分（无旧向量串味）。
    results = index.retrieve("PROFIT 利润", rerank=False)
    assert results
    assert results[0].vector_score > 0.0


def test_injected_vector_store_shared(store):
    """注入的 store 被 NarrativeIndex 持有并跨 retrieve 共享填充。"""
    vs = InProcessVectorStore()
    index = NarrativeIndex(
        store, embedding_backend=DeterministicEmbeddingBackend(dim=32), vector_store=vs
    )
    index.ingest("营收 增长 数据", _meta("doc_fin"))
    index.retrieve("营收", rerank=False)
    assert index.vector_store is vs
    assert vs.count() >= 1


def test_build_narrative_retriever_threads_vector_store(tmp_path):
    """build_narrative_retriever 把 vector_store 一路透传到 NarrativeIndex。"""
    vs = InProcessVectorStore()
    retriever, s = build_narrative_retriever(
        tmp_path / "chunks.db",
        embedding_backend=DeterministicEmbeddingBackend(dim=32),
        vector_store=vs,
    )
    try:
        assert retriever.index.vector_store is vs
    finally:
        s.close()


def test_build_narrative_retriever_default_vector_store_none(tmp_path):
    """默认（无 embedding 后端、无 vector_store）：纯 BM25，vector_store 为 None。"""
    retriever, s = build_narrative_retriever(tmp_path / "chunks.db")
    try:
        assert retriever.index.vector_store is None
    finally:
        s.close()
