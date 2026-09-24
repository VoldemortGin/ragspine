"""标题进索引开关接线（RAGSPINE_CONTEXTUAL_INDEX / ServiceConfig.contextual_index / RetrievalPreset）与持久化向量。

向量的索引文本随开关变化，旧向量不再对应新的索引文本：
- 向量库 doc 签名按【索引文本】计算，同步时签名变了的 doc 自动重嵌（off 时签名与原来逐字节一致）；
- 向量库记下索引文本版本（``contextual_index``，旧库缺省即 off）；检索期版本不一致抛
  ``VectorIndexMismatchError``（要求重新入库同步），绝不静默混用；
- 同步中途失败时版本处于「迁移中」，检索期无论哪个开关都拒绝使用。
"""

from pathlib import Path

import pytest

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend

from ..retrieval.contextual_index.conftest import DOC, load_heading_corpus

_MD = (
    "# New Business Profile\n\n## Distribution Mix\n\n<figure>\nAgency\n72%\nPartnerships\n28%\n"
    "</figure>\n\n<!-- PageBreak -->\n\n# Outlook\n\nAgency channel outlook remains positive.\n"
)


class _SpyBackend(DeterministicEmbeddingBackend):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.fail = fail

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding server down")
        self.texts += texts
        return super().embed_texts(texts)


def _store(tmp_path: Path) -> Path:
    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    try:
        load_heading_corpus(store)
    finally:
        store.close()
    return db


def _config(db: Path, mode: str):
    from ragspine.service.config import ServiceConfig

    return ServiceConfig(
        db_path=str(db),
        chunk_db_path=str(db),
        embedding="deterministic",
        persist_vectors=True,
        contextual_index=mode,
    )


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_service_config_default_and_env():
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig(db_path="x.db").contextual_index == "off"
    assert ServiceConfig.from_env({}).contextual_index == "off"
    env = {"RAGSPINE_CONTEXTUAL_INDEX": "heading"}
    assert ServiceConfig.from_env(env).contextual_index == "heading"


@pytest.mark.parametrize("mode", ["off", "heading", "full"])
def test_open_narrative_retriever_threads_the_switch(tmp_path, mode):
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.retrieval.contextual import make_index_text_fn
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = str(tmp_path / "k.db")
    config = ServiceConfig(db_path=db, chunk_db_path=db, embedding="none", contextual_index=mode)
    with open_narrative_retriever(config, MockProvider()) as retriever:
        assert retriever.index.index_text_fn is make_index_text_fn(mode)


def test_build_narrative_retriever_rejects_unknown_mode(tmp_path):
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever

    with pytest.raises(ValueError):
        build_narrative_retriever(tmp_path / "k.db", contextual_index="titles")


def test_facade_threads_the_switch(tmp_path, monkeypatch):
    import ragspine.session as session_module
    from ragspine import RAGSpine
    from ragspine.agent.agent import AgentResult
    from ragspine.service.config import make_retrieval_preset

    assert make_retrieval_preset().contextual_index == "off"
    assert RAGSpine.local(tmp_path / "ws")._service_config().contextual_index == "off"

    captured: list[list[dict[str, object]]] = []

    def fake_answer(question, store, provider, *, reference_date, narrative_retriever):
        captured.append(narrative_retriever.retrieve(question))
        return AgentResult(answer="", route="narrative", sources=[])

    monkeypatch.setattr(session_module, "answer_question", fake_answer)
    doc = tmp_path / "deck.md"
    doc.write_text(_MD, encoding="utf-8")
    rag = RAGSpine.local(
        tmp_path / "ws", retrieval=make_retrieval_preset(contextual_index="heading")
    )
    assert rag._service_config().contextual_index == "heading"
    rag.ingest(doc)
    rag.ask("distribution mix")
    assert captured[-1], "标题词应命中图表块"
    assert "Agency" in str(captured[-1][0]["text"])


# ---------------------------------------------------------------------------
# 持久化向量：签名 / 版本 / 重建
# ---------------------------------------------------------------------------


def test_heading_mode_embeds_the_index_text(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex

    db = _store(tmp_path)
    store = ChunkStore(db)
    chunks = store.iter_chunks()
    store.close()
    backend = _SpyBackend()
    index = ChunkVectorIndex(tmp_path / "v.db")
    try:
        index.sync(chunks, backend, model_id="det", contextual_index="heading")
        assert index.contextual_index == "heading"
    finally:
        index.close()
    assert any(
        t.startswith("[章节:New Business Profile > Distribution Mix]") for t in backend.texts
    )
    assert not any("Falcon" in t for t in backend.texts), "RESTRICTED 块不嵌入"


def test_switching_mode_reembeds_changed_docs_only(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex

    db = _store(tmp_path)
    store = ChunkStore(db)
    chunks = store.iter_chunks()
    store.close()
    index = ChunkVectorIndex(tmp_path / "v.db")
    try:
        first = index.sync(chunks, _SpyBackend(), model_id="det")
        assert index.contextual_index == "off"
        switched = index.sync(chunks, _SpyBackend(), model_id="det", contextual_index="heading")
        # deck.md 有标题 -> 索引文本变了，重嵌；legacy.pdf 没有标题 -> 索引文本不变，跳过。
        assert (switched.embedded, switched.deleted, switched.unchanged_docs) == (4, 4, 1)
        assert switched.total == first.total == 5
        again = index.sync(chunks, _SpyBackend(), model_id="det", contextual_index="heading")
        assert (again.embedded, again.unchanged_docs) == (0, 2)
        back = index.sync(chunks, _SpyBackend(), model_id="det", contextual_index="off")
        assert (back.embedded, index.contextual_index) == (4, "off")
    finally:
        index.close()


def test_query_side_rejects_a_different_index_text(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.retrieval.vector.chunk_index import VectorIndexMismatchError
    from ragspine.service.config import index_narrative_vectors, open_narrative_retriever

    db = _store(tmp_path)
    index_narrative_vectors(_config(db, "off"))
    with open_narrative_retriever(_config(db, "off"), MockProvider()):
        pass
    with pytest.raises(VectorIndexMismatchError, match="contextual_index|重建"):
        with open_narrative_retriever(_config(db, "heading"), MockProvider()):
            pass
    report = index_narrative_vectors(_config(db, "heading"))
    assert report is not None and report.embedded == 4
    with open_narrative_retriever(_config(db, "heading"), MockProvider()) as retriever:
        snippets = retriever.retrieve("distribution mix")
    assert snippets[0]["chunk_id"] == f"{DOC}#c1"
    assert snippets[0]["scores"]["vector"] > 0.0
    with pytest.raises(VectorIndexMismatchError):
        with open_narrative_retriever(_config(db, "off"), MockProvider()):
            pass


def test_legacy_vector_db_counts_as_off(tmp_path):
    pytest.importorskip("sqlite_vec")
    import sqlite3

    from ragspine.agent.llm_provider import MockProvider
    from ragspine.service.config import index_narrative_vectors, open_narrative_retriever

    db = _store(tmp_path)
    index_narrative_vectors(_config(db, "off"))
    conn = sqlite3.connect(tmp_path / "chunks.vectors.db")
    keys = [r[0] for r in conn.execute("SELECT key FROM chunk_vector_meta")]
    conn.close()
    assert "contextual_index" not in keys, "off 不写版本键，旧库内容不变"
    with open_narrative_retriever(_config(db, "off"), MockProvider()):
        pass


def test_interrupted_resync_is_never_used(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex, VectorIndexMismatchError

    db = _store(tmp_path)
    store = ChunkStore(db)
    chunks = store.iter_chunks()
    store.close()
    index = ChunkVectorIndex(tmp_path / "v.db")
    try:
        index.sync(chunks, _SpyBackend(), model_id="det")
        with pytest.raises(RuntimeError):
            index.sync(chunks, _SpyBackend(fail=True), model_id="det", contextual_index="heading")
        for mode in ("off", "heading"):
            with pytest.raises(VectorIndexMismatchError):
                index.check_compatible("det", contextual_index=mode)
        index.sync(chunks, _SpyBackend(), model_id="det", contextual_index="heading")
        index.check_compatible("det", contextual_index="heading")
    finally:
        index.close()


def test_worker_job_uses_the_switch(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    doc = tmp_path / "deck.md"
    doc.write_text(_MD, encoding="utf-8")
    run_narrative_ingest_job(
        {
            "inputs": [str(doc)],
            "chunk_db_path": str(tmp_path / "chunks.db"),
            "persist_vectors": True,
            "embedding": "deterministic",
            "contextual_index": "heading",
        }
    )
    index = ChunkVectorIndex(tmp_path / "chunks.vectors.db")
    try:
        assert index.contextual_index == "heading"
    finally:
        index.close()


def test_http_ingest_route_forwards_the_switch_to_the_worker(tmp_path):
    pytest.importorskip("sqlite_vec")
    from fastapi.testclient import TestClient

    from ragspine.agent.llm_provider import MockProvider
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex
    from ragspine.service.api.app import create_app
    from ragspine.service.config import ServiceConfig
    from ragspine.service.faq.faq_cache import FAQCache
    from ragspine.service.tasks.task_queue import JOB_FINISHED, FakeQueue

    doc = tmp_path / "deck.md"
    doc.write_text(_MD, encoding="utf-8")
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(tmp_path / "chunks.db"),
        embedding="deterministic",
        persist_vectors=True,
        contextual_index="heading",
    )
    queue = FakeQueue()
    app = create_app(config, provider=MockProvider(), queue=queue, faq_cache=FAQCache.empty())
    resp = TestClient(app).post("/v1/ingest/narrative/jobs", json={"inputs": [str(doc)]})
    assert resp.status_code == 200, resp.text
    assert queue.get(resp.json()["job_id"]).status == JOB_FINISHED
    index = ChunkVectorIndex(tmp_path / "chunks.vectors.db")
    try:
        assert index.contextual_index == "heading"
    finally:
        index.close()
