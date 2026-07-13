"""检索模式预设（批次 2.2 ④）单测：economy（零 embedding）vs 混合模式的显式切换。

钉死合约：
- make_retrieval_mode 别名 / env / 默认解析（大小写 / 连字符不敏感）；未知 spec 报 ValueError。
- 默认（auto/none）= 混合模式（uses_embedding=True），字节不变。
- economy 家族（economy/bm25/lexical/keyword）= 零 embedding（uses_embedding=False）。
- 经 ServiceConfig.retrieval_mode 在同一配置面切换；economy 装配路径【绝不构造/调用】embedding 后端，
  仍走 NarrativeIndexRetriever 出口（RESTRICTED 剔除 + provenance 完整）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.retrieval.mode import (
    ECONOMY,
    HYBRID,
    RETRIEVAL_MODE_ENV,
    make_retrieval_mode,
)
from ragspine.service.config import ServiceConfig, open_narrative_retriever


def test_default_is_hybrid():
    assert make_retrieval_mode(None) is HYBRID
    assert make_retrieval_mode("auto") is HYBRID
    assert make_retrieval_mode("none") is HYBRID
    assert HYBRID.uses_embedding is True


def test_economy_aliases_zero_embedding():
    for spec in ("economy", "bm25", "lexical", "keyword", "ECONOMY", "BM25"):
        mode = make_retrieval_mode(spec)
        assert mode is ECONOMY
        assert mode.uses_embedding is False


def test_hybrid_aliases():
    for spec in ("hybrid", "vector", "dense", "Hybrid"):
        assert make_retrieval_mode(spec) is HYBRID


def test_env_fallback(monkeypatch):
    monkeypatch.setenv(RETRIEVAL_MODE_ENV, "economy")
    assert make_retrieval_mode(None) is ECONOMY
    monkeypatch.delenv(RETRIEVAL_MODE_ENV, raising=False)
    assert make_retrieval_mode(None) is HYBRID


def test_unknown_spec_raises():
    with pytest.raises(ValueError):
        make_retrieval_mode("turbo")


def test_config_default_retrieval_mode_is_auto():
    cfg = ServiceConfig(db_path="/tmp/x.db")
    assert cfg.retrieval_mode == "auto"


def test_economy_config_never_constructs_embedding(monkeypatch, tmp_path):
    """economy 模式装配时【绝不调用】make_embedding_backend / make_vector_store（零 embedding 成本）。"""
    import ragspine.service.config as config_mod

    called = {"embed": 0, "vstore": 0}

    def _boom_embed(spec):
        called["embed"] += 1
        raise AssertionError("economy 模式不应构造 embedding 后端")

    def _boom_vstore(spec):
        called["vstore"] += 1
        raise AssertionError("economy 模式不应构造向量库")

    monkeypatch.setattr(config_mod, "make_embedding_backend", _boom_embed)
    monkeypatch.setattr(config_mod, "make_vector_store", _boom_vstore)

    cfg = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(tmp_path / "chunk.db"),
        retrieval_mode="economy",
        embedding="deterministic",   # 即便配了 embedding，economy 也必须无视之
        vector_store="in_process",
    )
    with open_narrative_retriever(cfg, MockProvider()) as retriever:
        assert retriever is not None
    assert called == {"embed": 0, "vstore": 0}


def test_economy_retrieval_strips_restricted_and_keeps_provenance(tmp_path):
    """economy 就是既有 BM25 默认通路：RESTRICTED 出口剔除 + 每 snippet 带 doc_id/locator。"""
    chunk_db = tmp_path / "chunk.db"
    store = ChunkStore(chunk_db)
    store.init_schema()
    pub = chunk_document(
        "营收增长强劲，毛利率提升。",
        DocumentMeta(doc_id="pub.pdf", topic="FIN", sensitivity="INTERNAL"),
    )
    sec = chunk_document(
        "营收机密评级 A。",
        DocumentMeta(doc_id="secret.pdf", topic="FIN", sensitivity="RESTRICTED"),
    )
    store.replace_doc_chunks("pub.pdf", pub)
    store.replace_doc_chunks("secret.pdf", sec)
    store.close()

    cfg = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(chunk_db),
        retrieval_mode="economy",
    )
    with open_narrative_retriever(cfg, MockProvider()) as retriever:
        snippets = retriever.retrieve("营收", top_k=50)
    # RESTRICTED 绝不出域。
    assert all(s["doc_id"] != "secret.pdf" for s in snippets)
    assert all(str(s["sensitivity"]).upper() != "RESTRICTED" for s in snippets)
    # 命中的公开块带完整 provenance。
    if snippets:
        assert all(s["doc_id"] and s["source_locator"] for s in snippets)
