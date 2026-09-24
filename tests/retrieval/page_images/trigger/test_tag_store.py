"""页标签表：入库写表（经 sync_ingested_page_images）、签名幂等、旧库懒计算、未关联 PDF 的库不建表。"""

import hashlib
import logging
import os
from pathlib import Path

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.page_tags import PAGE_TAGS_VERSION, PageTagStats
from ragspine.ingestion.narrative.narrative_ingest import ingest_narrative
from ragspine.ingestion.page_images.index import sync_ingested_page_images
from ragspine.ingestion.page_images.source_pdf import prepare_source_pdfs
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.page_images.trigger.tag_store import (
    TAG_SOURCE_LAZY,
    TAG_SOURCE_STORED,
    TAG_SOURCE_UNTAGGED,
    PageTagStore,
    clear_lazy_tag_cache,
    load_doc_tags,
)
from tests.ingestion.page_images.fixtures import write_deck

_LONG = "Operating profit grew strongly across every reporting segment this half. " * 6
_PAGES = [
    _LONG,
    "<table><tr><th>Year</th><th>VONB</th></tr><tr><td>1H26</td><td>3,212</td></tr></table>",
    "Short page.",
]


def _tables(db) -> set[str]:
    store = ChunkStore(db)
    try:
        return {
            r[0] for r in store.execute_read("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        store.close()


def _ingest(tmp_path, *, with_pdf: bool = True, pages=None):
    md, pdf = write_deck(tmp_path, pages or _PAGES)
    db = tmp_path / "knowledge.db"
    store = ChunkStore(db)
    store.init_schema()
    try:
        report = ingest_narrative([md], store)
    finally:
        store.close()
    sources = prepare_source_pdfs([md], pdf if with_pdf else None)
    sync_ingested_page_images(report, sources, db)
    return md, pdf, db


def test_store_roundtrip_and_signature(tmp_path):
    db = tmp_path / "k.db"
    store = PageTagStore(db)
    try:
        assert store.get_doc("deck.md") is None
        assert not store.has_schema()  # 读不建表
        stats = [PageTagStats(1, True, 0, 0, 12), PageTagStats(2, False, 2, 40, 900)]
        assert store.replace_doc("deck.md", stats, md_sha256="a" * 64) == 2
        assert store.doc_signature("deck.md") == f"{'a' * 64}|v{PAGE_TAGS_VERSION}"
        assert store.get_doc("deck.md") == {1: stats[0], 2: stats[1]}
        assert store.clear_doc("deck.md") == 2
        assert store.get_doc("deck.md") is None
    finally:
        store.close()


def test_ingest_with_pdf_writes_tags(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        md, _, db = _ingest(tmp_path)
    store = PageTagStore(db)
    try:
        tags = store.get_doc("deck.md")
        sig = store.doc_signature("deck.md")
    finally:
        store.close()
    assert tags is not None and sorted(tags) == [1, 2, 3]
    assert tags[2].has_table and not tags[1].has_table
    assert tags[1].text_chars >= 300 and tags[3].text_chars < 300
    md_sha = hashlib.sha256(md.read_bytes()).hexdigest()
    assert sig == f"{md_sha}|v{PAGE_TAGS_VERSION}"
    rec = [r for r in caplog.records if getattr(r, "op", "") == "narrative.page_tag_index"]
    assert len(rec) == 1 and (rec[0].written, rec[0].unchanged, rec[0].n_pages) == (1, 0, 3)


def test_reingest_is_idempotent(tmp_path, caplog):
    _ingest(tmp_path)
    with caplog.at_level(logging.INFO):
        _ingest(tmp_path)
    rec = [r for r in caplog.records if getattr(r, "op", "") == "narrative.page_tag_index"]
    assert (rec[-1].written, rec[-1].unchanged) == (0, 1)


def test_changed_markdown_rewrites_tags(tmp_path):
    _ingest(tmp_path)
    _ingest(tmp_path, pages=["Short.", "Short.", _LONG])
    store = PageTagStore(tmp_path / "knowledge.db")
    try:
        tags = store.get_doc("deck.md")
    finally:
        store.close()
    assert tags is not None
    assert [tags[p].text_chars >= 300 for p in (1, 2, 3)] == [False, False, True]


def test_ingest_without_pdf_creates_no_table(tmp_path):
    _, _, db = _ingest(tmp_path, with_pdf=False)
    assert not any(t.startswith("page_tag") for t in _tables(db))


def test_reingest_without_pdf_clears_tags(tmp_path):
    _ingest(tmp_path)
    _, _, db = _ingest(tmp_path, with_pdf=False)
    store = PageTagStore(db)
    try:
        assert store.get_doc("deck.md") is None
    finally:
        store.close()


def _legacy_db(tmp_path):
    """旧库：块与页图都在，但没有 page_tag 表（引入标签之前入库的）。"""
    md, _, db = _ingest(tmp_path)
    store = PageTagStore(db)
    try:
        store.clear_doc("deck.md")
    finally:
        store.close()
    clear_lazy_tag_cache()
    return md, db


def test_lazy_tags_when_source_file_matches(tmp_path):
    _, db = _legacy_db(tmp_path)
    store = PageTagStore(db)
    try:
        tags, source = load_doc_tags(store, "deck.md")
        assert source == TAG_SOURCE_LAZY
        assert tags is not None and tags[2].has_table
        # 只读：不回写
        assert store.get_doc("deck.md") is None
    finally:
        store.close()


def test_lazy_tags_hash_mismatch_is_untagged(tmp_path):
    md, db = _legacy_db(tmp_path)
    md.write_text("changed after ingest", encoding="utf-8")
    store = PageTagStore(db)
    try:
        assert load_doc_tags(store, "deck.md") == (None, TAG_SOURCE_UNTAGGED)
    finally:
        store.close()


def test_lazy_tags_missing_file_is_untagged(tmp_path):
    md, db = _legacy_db(tmp_path)
    md.unlink()
    store = PageTagStore(db)
    try:
        assert load_doc_tags(store, "deck.md") == (None, TAG_SOURCE_UNTAGGED)
        assert load_doc_tags(store, "nope.md") == (None, TAG_SOURCE_UNTAGGED)
    finally:
        store.close()


def test_stored_tags_win(tmp_path):
    _, _, db = _ingest(tmp_path)
    store = PageTagStore(db)
    try:
        tags, source = load_doc_tags(store, "deck.md")
    finally:
        store.close()
    assert source == TAG_SOURCE_STORED and tags is not None and len(tags) == 3


def test_lazy_tags_with_relative_source_path_depend_on_cwd(tmp_path, monkeypatch):
    """台账按入库时收到的原样记 source_path：相对路径按当前 cwd 解析，换 cwd 就是 untagged（安全：只会少附图）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_deck(repo, _PAGES)
    db = repo / "knowledge.db"
    monkeypatch.chdir(repo)
    store = ChunkStore(db)
    store.init_schema()
    try:
        ingest_narrative([Path("deck.md")], store)  # 相对路径入库
    finally:
        store.close()
    clear_lazy_tag_cache()
    tags = PageTagStore(db)
    try:
        assert tags.registered_source("deck.md")[0] == "deck.md"
        assert load_doc_tags(tags, "deck.md")[1] == TAG_SOURCE_LAZY
        clear_lazy_tag_cache()
        monkeypatch.chdir(tmp_path)
        assert load_doc_tags(tags, "deck.md") == (None, TAG_SOURCE_UNTAGGED)
    finally:
        tags.close()
