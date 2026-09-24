"""持久化块向量索引（优化1）：入库即嵌入落盘，检索期读同一个库。

复现的生产 bug：``RAGSpine.ingest`` 只写 chunk、``RAGSpine.ask`` 用空的内存向量库，混合检索的向量
通道实际为空（只剩 BM25）。开关 ``storage.persist_vectors`` / ``RAGSPINE_PERSIST_VECTORS`` 打开后：
入库按配置的 embedding 后端给 chunk 生成向量写进 sqlite-vec 文件，检索从该文件读；默认关，旧行为不变。
"""

import logging
from pathlib import Path

import pytest

from ragspine.common.sensitivity import RESTRICTED
from ragspine.ingestion.narrative.narrative_ingest import ingest_narrative
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend

pytest.importorskip("sqlite_vec")

# 三段、每段约 400 字符：默认切块（max_chars=480）下正好三个块。
_TEXT = "\n\n".join(
    " ".join([sentence] * 6)
    for sentence in (
        "Agency channel expansion lifted new business value in Hong Kong.",
        "Bancassurance partnerships in Thailand grew steadily this period.",
        "Operating profit after tax improved across the whole group.",
    )
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _capture_snippets(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, object]]]:
    """把 session.answer_question 换成只做一次叙事检索的探针，收集检索出的 snippet。"""
    import ragspine.session as session_module
    from ragspine.agent.agent import AgentResult

    captured: list[list[dict[str, object]]] = []

    def fake_answer(question, store, provider, *, reference_date, narrative_retriever):
        captured.append(narrative_retriever.retrieve(question))
        return AgentResult(answer="", route="narrative", sources=[])

    monkeypatch.setattr(session_module, "answer_question", fake_answer)
    return captured


def _trace_records(caplog: pytest.LogCaptureFixture, op: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "op", None) == op]


# ---------------------------------------------------------------------------
# 复现：session ingest -> ask 的向量通道
# ---------------------------------------------------------------------------


def test_session_ingest_then_ask_populates_vector_channel(tmp_path, monkeypatch):
    from ragspine import RAGSpine

    doc = _write(tmp_path, "notes.txt", _TEXT)
    captured = _capture_snippets(monkeypatch)
    rag = RAGSpine.local(
        tmp_path / "ws", preset="balanced", config={"storage": {"persist_vectors": True}}
    )
    result = rag.ingest(doc)
    assert result.vector_report is not None
    assert result.vector_report.embedded == 3
    assert (tmp_path / "ws" / "knowledge.vectors.db").is_file()

    rag.ask("agency channel new business value")

    snippets = captured[0]
    assert snippets, "BM25 与向量通道都应有命中"
    assert any(s["scores"]["vector"] > 0.0 for s in snippets)


def test_session_default_keeps_old_behavior_without_vector_file(tmp_path, monkeypatch):
    """开关默认关：不生成向量文件，向量通道与改动前一致（balanced 下仍是空的内存库）。"""
    from ragspine import RAGSpine

    doc = _write(tmp_path, "notes.txt", _TEXT)
    captured = _capture_snippets(monkeypatch)
    rag = RAGSpine.local(tmp_path / "ws", preset="balanced")
    result = rag.ingest(doc)
    assert result.vector_report is None
    assert not (tmp_path / "ws" / "knowledge.vectors.db").exists()

    rag.ask("agency channel new business value")
    assert all(s["scores"]["vector"] == 0.0 for s in captured[0])


# ---------------------------------------------------------------------------
# 幂等 / 替换
# ---------------------------------------------------------------------------


def test_reingesting_same_file_does_not_duplicate_vectors(tmp_path):
    from ragspine import RAGSpine

    doc = _write(tmp_path, "notes.txt", _TEXT)
    rag = RAGSpine.local(
        tmp_path / "ws", preset="balanced", config={"storage": {"persist_vectors": True}}
    )
    first = rag.ingest(doc).vector_report
    second = rag.ingest(doc).vector_report
    assert first is not None and second is not None
    assert (first.embedded, first.total) == (3, 3)
    assert (second.embedded, second.deleted, second.total) == (0, 0, 3)


def test_replaced_chunks_replace_their_vectors(tmp_path):
    from ragspine import RAGSpine
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex

    doc = _write(tmp_path, "notes.txt", _TEXT)
    rag = RAGSpine.local(
        tmp_path / "ws", preset="balanced", config={"storage": {"persist_vectors": True}}
    )
    rag.ingest(doc)
    doc.write_text("Only one paragraph remains about embedded value.", encoding="utf-8")
    report = rag.ingest(doc).vector_report
    assert report is not None
    assert (report.deleted, report.embedded, report.total) == (3, 1, 1)

    index = ChunkVectorIndex(tmp_path / "ws" / "knowledge.vectors.db")
    try:
        hits = index.store.query(DeterministicEmbeddingBackend().embed_texts(["value"])[0], k=10)
    finally:
        index.close()
    assert [h.id for h in hits] == ["notes.txt#c0"]


# ---------------------------------------------------------------------------
# RESTRICTED / 降级 / 模型标识
# ---------------------------------------------------------------------------


def test_restricted_chunks_are_not_embedded(tmp_path, caplog):
    from ragspine.service.config import ServiceConfig, index_narrative_vectors

    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative(
            [_write(tmp_path, "open.txt", _TEXT), _write(tmp_path, "secret.txt", _TEXT)],
            store,
            meta_by_doc={
                "open.txt": {"sensitivity": "INTERNAL"},
                "secret.txt": {"sensitivity": RESTRICTED},
            },
        )
    finally:
        store.close()

    caplog.set_level(logging.INFO, logger="ragspine.trace")
    config = ServiceConfig(
        db_path=str(db), chunk_db_path=str(db), embedding="deterministic", persist_vectors=True
    )
    report = index_narrative_vectors(config)
    assert report is not None
    assert (report.embedded, report.withheld, report.total) == (3, 3, 3)
    (trace,) = _trace_records(caplog, "narrative.vector_index")
    assert trace.vector_channel == "hybrid"
    assert trace.withheld == 3


def test_missing_embedding_backend_degrades_to_bm25_with_trace(tmp_path, caplog):
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.service.config import (
        ServiceConfig,
        index_narrative_vectors,
        open_narrative_retriever,
    )

    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative([_write(tmp_path, "notes.txt", _TEXT)], store)
    finally:
        store.close()

    caplog.set_level(logging.INFO, logger="ragspine.trace")
    config = ServiceConfig(
        db_path=str(db), chunk_db_path=str(db), embedding="none", persist_vectors=True
    )
    report = index_narrative_vectors(config)
    assert report is not None
    assert (report.vector_channel, report.embedded) == ("bm25_only", 0)
    assert not Path(str(db).removesuffix(".db") + ".vectors.db").exists()

    with open_narrative_retriever(config, MockProvider()) as retriever:
        assert retriever is not None
        snippets = retriever.retrieve("agency channel")
    assert snippets and all(s["scores"]["vector"] == 0.0 for s in snippets)

    (ingest_trace,) = _trace_records(caplog, "narrative.vector_index")
    assert (ingest_trace.vector_channel, ingest_trace.vector_reason) == (
        "bm25_only",
        "no_embedding_backend",
    )
    assert ingest_trace.n_chunks == 3
    (query_trace,) = _trace_records(caplog, "narrative.vector_channel")
    assert (query_trace.vector_channel, query_trace.vector_reason, query_trace.n_vectors) == (
        "bm25_only",
        "no_embedding_backend",
        0,
    )


def test_query_reports_hybrid_channel_with_vector_count(tmp_path, caplog):
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.service.config import (
        ServiceConfig,
        index_narrative_vectors,
        open_narrative_retriever,
    )

    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative([_write(tmp_path, "notes.txt", _TEXT)], store)
    finally:
        store.close()
    config = ServiceConfig(
        db_path=str(db), chunk_db_path=str(db), embedding="deterministic", persist_vectors=True
    )
    index_narrative_vectors(config)

    caplog.set_level(logging.INFO, logger="ragspine.trace")
    with open_narrative_retriever(config, MockProvider()) as retriever:
        assert retriever is not None
        snippets = retriever.retrieve("agency channel")
    assert any(s["scores"]["vector"] > 0.0 for s in snippets)
    (trace,) = _trace_records(caplog, "narrative.vector_channel")
    assert (trace.vector_channel, trace.n_vectors) == ("hybrid", 3)


def test_model_identity_and_dimension_are_checked(tmp_path):
    from ragspine.retrieval.vector.chunk_index import (
        ChunkVectorIndex,
        VectorIndexMismatchError,
        embedding_model_id,
    )

    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative([_write(tmp_path, "notes.txt", _TEXT)], store)
        chunks = store.iter_chunks()
    finally:
        store.close()

    small = DeterministicEmbeddingBackend(dim=32)
    other = DeterministicEmbeddingBackend(dim=64)
    assert embedding_model_id(small) != embedding_model_id(other)

    index = ChunkVectorIndex(tmp_path / "v.db")
    try:
        index.sync(chunks, small, model_id=embedding_model_id(small))
        assert (index.model_id, index.dim) == (embedding_model_id(small), 32)
        index.check_compatible(embedding_model_id(small))
        with pytest.raises(VectorIndexMismatchError, match="rebuild|重建"):
            index.check_compatible(embedding_model_id(other))
        with pytest.raises(VectorIndexMismatchError):
            index.sync(chunks, other, model_id=embedding_model_id(other))
        assert index.count() == 3
    finally:
        index.close()


def test_query_side_rejects_a_different_embedding_model(tmp_path, monkeypatch):
    import ragspine.service.config as service_config
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.retrieval.vector.chunk_index import VectorIndexMismatchError

    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative([_write(tmp_path, "notes.txt", _TEXT)], store)
    finally:
        store.close()
    config = service_config.ServiceConfig(
        db_path=str(db), chunk_db_path=str(db), embedding="deterministic", persist_vectors=True
    )
    service_config.index_narrative_vectors(config)

    monkeypatch.setattr(
        service_config,
        "make_embedding_backend",
        lambda spec: DeterministicEmbeddingBackend(dim=64),
    )
    with pytest.raises(VectorIndexMismatchError):
        with service_config.open_narrative_retriever(config, MockProvider()):
            pass


# ---------------------------------------------------------------------------
# 接线：配置 / worker job
# ---------------------------------------------------------------------------


def test_switch_defaults_off_and_reads_env():
    from ragspine.config import RAGSpineConfig
    from ragspine.service.config import ServiceConfig

    assert RAGSpineConfig().storage.persist_vectors is False
    assert ServiceConfig(db_path="x.db").persist_vectors is False
    assert ServiceConfig(db_path="x.db").vector_db_path is None
    cfg = ServiceConfig.from_env(
        {"RAGSPINE_PERSIST_VECTORS": "true", "RAGSPINE_VECTOR_DB_PATH": "v.db"}
    )
    assert (cfg.persist_vectors, cfg.vector_db_path) == (True, "v.db")


def test_worker_job_embeds_chunks_when_switch_is_on(tmp_path):
    from ragspine.retrieval.vector.chunk_index import ChunkVectorIndex
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    db = tmp_path / "chunks.db"
    report = run_narrative_ingest_job(
        {
            "inputs": [str(_write(tmp_path, "notes.txt", _TEXT))],
            "chunk_db_path": str(db),
            "persist_vectors": True,
            "embedding": "deterministic",
        }
    )
    assert report["vectors"]["embedded"] == 3
    index = ChunkVectorIndex(tmp_path / "chunks.vectors.db")
    try:
        assert index.count() == 3
    finally:
        index.close()


def test_worker_job_report_unchanged_when_switch_is_off(tmp_path):
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    db = tmp_path / "chunks.db"
    report = run_narrative_ingest_job(
        {"inputs": [str(_write(tmp_path, "notes.txt", _TEXT))], "chunk_db_path": str(db)}
    )
    assert "vectors" not in report
    assert not (tmp_path / "chunks.vectors.db").exists()
