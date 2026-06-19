"""敏感度门控持久化增量：入库即嵌入落盘 + doc 粒度失效 + PersistencePolicy 隔离绑定。

NarrativeIndex 有 embedding 后端时改为【入库即嵌入并写进 vector_store】（policy 门控），
retrieve 走 store-managed（不重嵌块，只嵌 query）——持久化（sqlite-vec db_path）由此真正生效：
重启/换实例不重算块向量。隔离绑定：默认 IsolationFirstPolicy 绝不把 RESTRICTED 块向量落盘。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.persistence_policy import PersistEverythingPolicy


def _meta(doc_id: str, **overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id=doc_id, title=doc_id, topic="FIN", entity="ACME_HK",
        geography="HK", period="2025H1", language="zh", sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


class SpyBackend:
    """录下所有被 embedding 的文本（用 DeterministicEmbeddingBackend 出真向量）。"""

    def __init__(self, dim: int = 32):
        self.embedded: list[str] = []
        self._inner = DeterministicEmbeddingBackend(dim=dim)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return self._inner.embed_texts(texts)


@pytest.fixture
def store(tmp_db_path):
    s = ChunkStore(tmp_db_path)
    s.init_schema()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# 入库即嵌入落盘
# ---------------------------------------------------------------------------

def test_embed_and_persist_at_ingest(store):
    """入库即把可落盘块的向量写进 vector_store——尚未 retrieve，count 已等于块数。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    n = index.ingest("香港 REVENUE 营收持续增长 表现强劲", _meta("doc_fin"))
    assert n >= 1
    assert index.vector_store.count() == n  # 默认 policy 下全 INTERNAL 块落盘


def test_retrieve_does_not_reembed_chunks(store):
    """store-managed：retrieve 只嵌 query（及改写），绝不重嵌已落盘的块文本。"""
    spy = SpyBackend()
    index = NarrativeIndex(store, embedding_backend=spy)
    index.ingest("香港 REVENUE 营收持续增长", _meta("doc_fin"))
    chunk_texts = set(spy.embedded)
    assert chunk_texts  # 入库即嵌入
    spy.embedded.clear()
    results = index.retrieve("营收 增长", rerank=False)
    assert results and results[0].vector_score > 0.0  # 用的是已落盘向量
    assert not (set(spy.embedded) & chunk_texts)  # 块文本一个都没重嵌


# ---------------------------------------------------------------------------
# doc 粒度失效（持久 store 跨 ingest 存活）
# ---------------------------------------------------------------------------

def test_doc_scoped_invalidation_preserves_other_docs(store):
    """重入 docA 只撤换 docA 的向量，docB 的持久向量存活（不再 delete-all 全清）。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    index.ingest("docA 香港 REVENUE 营收", _meta("docA"))
    index.ingest("docB 利润 PROFIT 大增", _meta("docB", entity="ACME_CN"))
    assert index.vector_store.count() >= 2
    index.ingest("docA 新版 营收下滑", _meta("docA"))  # 重入 A
    # docB 向量未被全清——仍能以向量分召回。
    results = index.retrieve("利润 PROFIT", entity="ACME_CN", rerank=False)
    b_hits = [r for r in results if r.chunk.doc_id == "docB"]
    assert b_hits and b_hits[0].vector_score > 0.0


def test_reingest_replaces_only_that_doc_vectors(store):
    """重入同 doc：旧版本向量撤下、新版本向量入库，count 不累积重复。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    n1 = index.ingest("docA 第一版 营收 一二三", _meta("docA"))
    n2 = index.ingest("docA 第二版 营收 四五六七八九", _meta("docA"))
    # 只剩第二版的向量（活跃集 = 最新一批），无旧版残留。
    assert index.vector_store.count() == n2
    assert n2 != 0


# ---------------------------------------------------------------------------
# PersistencePolicy 隔离绑定：默认绝不落盘 RESTRICTED 向量
# ---------------------------------------------------------------------------

def test_restricted_vector_not_persisted_under_default_policy(store):
    """默认 IsolationFirstPolicy：RESTRICTED 块的衍生向量绝不写盘（at-rest 隔离第三道门）。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    index.ingest("机密 高管薪酬 SECRET 数据", _meta("doc_sec", sensitivity="RESTRICTED"))
    assert index.vector_store.count() == 0  # RESTRICTED 块向量一条都没落盘
    # 反证：INTERNAL 文档照常落盘。
    index.ingest("公开 香港 营收 增长", _meta("doc_pub", sensitivity="INTERNAL"))
    assert index.vector_store.count() >= 1


def test_restricted_chunk_still_retrievable_via_bm25_with_zero_vector(store):
    """RESTRICTED 块无持久向量 -> 仍被 BM25 召回（向量分=0）；权威剔除在 link/rerank 出口（不在此）。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    index.ingest("机密 SECRET 高管 薪酬 token", _meta("doc_sec", sensitivity="RESTRICTED"))
    results = index.retrieve("机密 SECRET", rerank=False)
    sec = [r for r in results if r.chunk.doc_id == "doc_sec"]
    assert sec  # BM25 仍召回
    assert sec[0].vector_score == 0.0  # 但无持久向量 -> 向量分 0


def test_reingest_escalating_to_restricted_sweeps_persisted_vector(store):
    """跨版本敏感度升级 INTERNAL -> RESTRICTED：重入后旧 INTERNAL 向量被 doc 粒度 delete 扫掉，
    新 RESTRICTED 版本被 IsolationFirstPolicy 拦下 -> 该 doc 在库内零向量（at-rest 无残留泄露）。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    index.ingest("营收 数据 一二三", _meta("doc_x", sensitivity="INTERNAL"))
    assert index.vector_store.count() >= 1  # INTERNAL 版本已落盘
    index.ingest("营收 机密 SECRET 升级", _meta("doc_x", sensitivity="RESTRICTED"))
    assert index.vector_store.count() == 0  # 旧向量扫除 + 新版被拦 -> 零残留


def test_reingest_fewer_chunks_sweeps_orphan_vectors(store):
    """重入块数变少：旧版本多出的块向量（孤儿）被 doc 粒度 delete 全扫，库内只剩新版本。"""
    index = NarrativeIndex(store, embedding_backend=DeterministicEmbeddingBackend(dim=32))
    # 多段、每段超长，确保切出 >=2 块（段落粒度 + max_chars 默认 480）。
    big = "\n\n".join(
        "营收增长与利润结构的详细分析段落内容，".replace("，", f"，第{i}节。") * 30
        for i in range(4)
    )
    n1 = index.ingest(big, _meta("doc_y"))
    assert n1 >= 2 and index.vector_store.count() == n1
    n2 = index.ingest("短 文档 一段", _meta("doc_y"))  # 明显更少的块
    assert n2 < n1
    assert index.vector_store.count() == n2  # 无旧版本孤儿向量残留


def test_persist_everything_policy_persists_restricted(store):
    """opt-in PersistEverythingPolicy：RESTRICTED 块向量照样落盘（整库已按 RESTRICTED-tier 保护时）。"""
    index = NarrativeIndex(
        store,
        embedding_backend=DeterministicEmbeddingBackend(dim=32),
        persistence_policy=PersistEverythingPolicy(),
    )
    index.ingest("机密 SECRET 高管薪酬", _meta("doc_sec", sensitivity="RESTRICTED"))
    assert index.vector_store.count() >= 1


# ---------------------------------------------------------------------------
# 持久化真正生效：跨实例（模拟重启）不重算块向量
# ---------------------------------------------------------------------------

def test_persistence_survives_across_index_instances(tmp_path):
    """换一个 NarrativeIndex 实例（同 sqlite-vec db）即可用已落盘向量检索——块文本不重嵌。"""
    pytest.importorskip("sqlite_vec", reason="sqlite-vec 未装（pip install ragspine[vector]）")
    from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore

    chunk_db = str(tmp_path / "chunks.db")
    vec_db = str(tmp_path / "vec.db")

    # 实例1：建库 + 入库（块向量落盘到 vec.db）。
    cs1 = ChunkStore(chunk_db)
    cs1.init_schema()
    vs1 = SqliteVecVectorStore(vec_db)
    idx1 = NarrativeIndex(cs1, embedding_backend=SpyBackend(), vector_store=vs1)
    idx1.ingest("香港 REVENUE 营收持续增长", _meta("doc_fin"))
    assert vs1.count() >= 1
    vs1.close()
    cs1.close()

    # 实例2（模拟重启）：同 db 重开，检索复用已落盘向量，块文本不重嵌。
    cs2 = ChunkStore(chunk_db)
    vs2 = SqliteVecVectorStore(vec_db)
    spy2 = SpyBackend()
    idx2 = NarrativeIndex(cs2, embedding_backend=spy2, vector_store=vs2)
    results = idx2.retrieve("REVENUE 营收", rerank=False)
    try:
        assert results and results[0].vector_score > 0.0  # 持久向量被复用
        assert spy2.embedded  # 只嵌入了 query（及改写）
        assert "香港 REVENUE 营收持续增长" not in "".join(spy2.embedded)  # 块文本未重嵌
    finally:
        vs2.close()
        cs2.close()
