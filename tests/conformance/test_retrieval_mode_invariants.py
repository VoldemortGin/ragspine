"""检索模式预设（批次 2.2 ④ economy）不变量绑定（conformance）。

对【每个检索模式】（economy＝零 embedding / auto＝混合）参数化断言核心不变量：
    - 隔离：经 A 线出口检索，结果绝不含 RESTRICTED。
    - provenance：每条 snippet 带非空 doc_id + source_locator。
    - 确定性：同 query 两次检索逐位一致。
economy 额外：装配路径【绝不构造/调用】embedding 后端 / 向量库（零 embedding 成本）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import ragspine.service.config as config_mod
from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.service.config import ServiceConfig, open_narrative_retriever

RETRIEVAL_MODES = ("economy", "auto")


def _chunk_db(tmp_path):
    chunk_db = tmp_path / "chunk.db"
    store = ChunkStore(chunk_db)
    store.init_schema()
    store.replace_doc_chunks(
        "pub.pdf", chunk_document("营收增长强劲，毛利率提升。", DocumentMeta(doc_id="pub.pdf", topic="FIN")))
    store.replace_doc_chunks(
        "sec.pdf", chunk_document("营收机密评级 A。", DocumentMeta(doc_id="sec.pdf", topic="FIN", sensitivity="RESTRICTED")))
    store.close()
    return chunk_db


@pytest.mark.parametrize("mode", RETRIEVAL_MODES, ids=RETRIEVAL_MODES)
def test_mode_isolation_provenance_determinism(tmp_path, mode):
    cfg = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(_chunk_db(tmp_path)),
        retrieval_mode=mode,
    )
    with open_narrative_retriever(cfg, MockProvider()) as retriever:
        out = retriever.retrieve("营收", top_k=50)
        again = retriever.retrieve("营收", top_k=50)
    # 隔离：RESTRICTED 绝不出域。
    assert all(str(s["sensitivity"]).upper() != "RESTRICTED" for s in out)
    assert all(s["doc_id"] != "sec.pdf" for s in out)
    # provenance：每条带 doc_id + source_locator。
    for s in out:
        assert s["doc_id"] and s["source_locator"]
    # 确定性。
    assert [s.get("chunk_id") for s in out] == [s.get("chunk_id") for s in again]


def test_economy_mode_zero_embedding(tmp_path, monkeypatch):
    """economy 模式【绝不】构造 embedding 后端 / 向量库（零 embedding 成本）。"""
    def _boom(spec):
        raise AssertionError("economy 模式不应构造 embedding/向量库")

    monkeypatch.setattr(config_mod, "make_embedding_backend", _boom)
    monkeypatch.setattr(config_mod, "make_vector_store", _boom)
    cfg = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(_chunk_db(tmp_path)),
        retrieval_mode="economy",
        embedding="deterministic",
        vector_store="in_process",
    )
    with open_narrative_retriever(cfg, MockProvider()) as retriever:
        assert retriever is not None
