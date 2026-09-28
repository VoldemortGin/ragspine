"""doc_id = 文件名：不同目录下的同名文件不得静默覆盖已入库文档。

复现的 bug：a/report.md 与 b/report.md（内容不同）依次入库，b 把 a 的块 / 台账 / 向量整体替换，
再入 a 又替换回来（幂等失效）。修法：doc_id 规则不变；台账里登记的来源文件仍在、且不是本文件时，
本文件记 failed 并报冲突，其余文件照常入库。来源文件已不在（搬家）或旧台账没记路径时按原语义更新。
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ragspine.ingestion.narrative.narrative_ingest import (
    STATUS_FAILED,
    STATUS_INGESTED,
    STATUS_SKIPPED,
    ingest_narrative,
)
from ragspine.ingestion.page_images.source_pdf import SourcePdfError, prepare_source_pdfs
from ragspine.retrieval.chunking.chunk_store import ChunkStore

from ..page_images.fixtures import make_md, make_pdf

_ALPHA = "# Alpha\nAlpha revenue grew strongly in Hong Kong this year.\n"
_BETA = "# Beta\nBeta bancassurance in Thailand declined sharply.\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _active_texts(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as conn, conn:
        rows = conn.execute(
            "SELECT text FROM narrative_chunk WHERE active = 1 ORDER BY doc_id, seq"
        ).fetchall()
    return [row[0] for row in rows]


def _ingest(db: Path, *paths: Path):
    store = ChunkStore(db)
    store.init_schema()
    try:
        return ingest_narrative(list(paths), store)
    finally:
        store.close()


def test_same_name_in_another_directory_is_refused_not_overwritten(tmp_path):
    db = tmp_path / "chunks.db"
    first = _write(tmp_path / "a" / "report.md", _ALPHA)
    second = _write(tmp_path / "b" / "report.md", _BETA)

    assert _ingest(db, first).files[0].status == STATUS_INGESTED
    rep = _ingest(db, second).files[0]

    assert rep.status == STATUS_FAILED
    assert rep.doc_id == "report.md"
    assert "report.md" in rep.error and str(first.resolve()) in rep.error
    assert any("Alpha" in text for text in _active_texts(db))
    assert not any("Beta" in text for text in _active_texts(db))
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("SELECT source_path FROM narrative_doc").fetchall() == [(str(first),)]
    # 冲突不破坏原文档的幂等：再入原文件仍是 skipped。
    assert _ingest(db, first).files[0].status == STATUS_SKIPPED


def test_identical_copy_elsewhere_is_refused_too(tmp_path):
    """一个 doc_id 只绑定一个来源文件——内容相同的副本也不能接管页图 / 标签等按 doc_id 挂的数据。"""
    db = tmp_path / "chunks.db"
    first = _write(tmp_path / "a" / "report.md", _ALPHA)
    copy = _write(tmp_path / "b" / "report.md", _ALPHA)
    _ingest(db, first)
    assert _ingest(db, copy).files[0].status == STATUS_FAILED


def test_collision_inside_one_batch_keeps_the_first_file(tmp_path):
    db = tmp_path / "chunks.db"
    first = _write(tmp_path / "a" / "report.md", _ALPHA)
    second = _write(tmp_path / "b" / "report.md", _BETA)
    other = _write(tmp_path / "b" / "other.md", _BETA)

    report = _ingest(db, first, second, other)

    assert [f.status for f in report.files] == [STATUS_INGESTED, STATUS_FAILED, STATUS_INGESTED]
    assert report.files[2].doc_id == "other.md"


def test_moved_source_updates_the_same_doc_id(tmp_path):
    """原来源文件已不在（搬家 / 删除）：按原语义更新同一个 doc_id，locator 不变。"""
    db = tmp_path / "chunks.db"
    old = _write(tmp_path / "a" / "report.md", _ALPHA)
    _ingest(db, old)
    old.unlink()
    moved = _write(tmp_path / "b" / "report.md", _BETA)

    rep = _ingest(db, moved).files[0]

    assert rep.status == STATUS_INGESTED
    assert _active_texts(db) and all("Beta" in text for text in _active_texts(db))


def test_same_path_update_still_replaces(tmp_path):
    db = tmp_path / "chunks.db"
    doc = _write(tmp_path / "a" / "report.md", _ALPHA)
    _ingest(db, doc)
    _write(doc, _BETA)
    assert _ingest(db, doc).files[0].status == STATUS_INGESTED
    assert all("Beta" in text for text in _active_texts(db))


def test_legacy_ledger_row_without_source_path_is_not_a_conflict(tmp_path):
    db = tmp_path / "chunks.db"
    first = _write(tmp_path / "a" / "report.md", _ALPHA)
    _ingest(db, first)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE narrative_doc SET source_path = ''")
    second = _write(tmp_path / "b" / "report.md", _BETA)
    assert _ingest(db, second).files[0].status == STATUS_INGESTED


def test_relative_ledger_path_matches_the_same_file(tmp_path, monkeypatch):
    """台账按收到的样子记路径：相对路径入库后再用绝对路径入同一文件，不算冲突。"""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "chunks.db"
    doc = _write(tmp_path / "a" / "report.md", _ALPHA)
    _ingest(db, Path("a") / "report.md")
    _write(doc, _BETA)
    assert _ingest(db, doc.resolve()).files[0].status == STATUS_INGESTED


def test_dry_run_reports_the_conflict(tmp_path):
    db = tmp_path / "chunks.db"
    _ingest(db, _write(tmp_path / "a" / "report.md", _ALPHA))
    second = _write(tmp_path / "b" / "report.md", _BETA)
    store = ChunkStore(db)
    store.init_schema()
    try:
        rep = ingest_narrative([second], store, dry_run=True).files[0]
    finally:
        store.close()
    assert rep.status == STATUS_FAILED


def test_facade_keeps_vectors_of_the_first_document(tmp_path):
    pytest.importorskip("sqlite_vec")
    from ragspine import RAGSpine

    first = _write(tmp_path / "a" / "report.md", _ALPHA)
    second = _write(tmp_path / "b" / "report.md", _BETA)
    rag = RAGSpine.local(
        tmp_path / "ws", preset="balanced", config={"storage": {"persist_vectors": True}}
    )
    rag.ingest(first)
    result = rag.ingest(second)

    assert result.failed
    assert result.vector_report is not None
    assert (result.vector_report.deleted, result.vector_report.embedded) == (0, 0)


def test_same_named_markdown_with_pdfs_in_one_batch_is_rejected_before_writes(tmp_path):
    """页图按 doc_id 挂：一批里两个同名 .md 都配了 PDF 时无法确定挂哪份，写入前报错。"""
    for sub in ("a", "b"):
        _write(tmp_path / sub / "report.md", make_md(["x"]))
        make_pdf(tmp_path / sub / "report.pdf", ["X"])
        _write(tmp_path / sub / "report.meta.json", '{"source_pdf": "report.pdf"}')
    with pytest.raises(SourcePdfError, match="report.md"):
        prepare_source_pdfs([tmp_path / "a" / "report.md", tmp_path / "b" / "report.md"])
