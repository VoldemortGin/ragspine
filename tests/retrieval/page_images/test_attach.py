"""检索期附页图：页级父子去重之后，前 N 条结果按 (doc_id, page) 查映射表附上页图引用。"""

import logging
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.page_images.attach import (
    DEFAULT_PAGE_IMAGES_TOP_N,
    PageImageRetriever,
    make_page_image_retriever,
    make_page_images_mode,
)
from ragspine.retrieval.page_images.store import PageImageStore, RenderedPage

from ..page_parent.conftest import load_page_corpus


def _png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "knowledge.db"
    store = ChunkStore(path)
    load_page_corpus(store)
    store.close()
    images = PageImageStore(path)
    images.replace_doc(
        "deck.md",
        pdf_sha256="f" * 64,
        pdf_pages=3,
        dpi=144,
        max_side=1568,
        signature="s",
        pages=[RenderedPage(p, _png(f"deck-{p}"), 10, 5) for p in (1, 2, 3)],
    )
    images.close()
    return path


def _base(db, mode="dedup"):
    store = ChunkStore(db)
    return NarrativeIndexRetriever(NarrativeIndex(store, page_parent=mode)), store


def test_mode_parsing_and_defaults():
    assert DEFAULT_PAGE_IMAGES_TOP_N == 3
    assert make_page_images_mode(None) == "off"
    assert make_page_images_mode("") == "off"
    assert make_page_images_mode("OFF") == "off"
    assert make_page_images_mode("on") == "on"
    with pytest.raises(ValueError):
        make_page_images_mode("maybe")


def test_off_returns_base_unchanged(db):
    base, store = _base(db)
    try:
        assert make_page_image_retriever(base, "off", chunk_db_path=db) is base
    finally:
        store.close()


def test_top_n_pages_get_image_refs_with_provenance(db):
    base, store = _base(db)
    try:
        retriever = make_page_image_retriever(
            base, "on", chunk_db_path=db, top_n=1, page_parent="dedup"
        )
        plain = base.retrieve("Hong Kong revenue commentary")
        snippets = retriever.retrieve("Hong Kong revenue commentary")
    finally:
        store.close()
    assert isinstance(retriever, PageImageRetriever)
    first = snippets[0]
    assert first["parent_locator"] == "deck.md@page=3"
    image = first["page_image"]
    assert image["doc_id"] == "deck.md" and image["page"] == 3
    assert image["path"].endswith(".png") and os.path.isfile(image["path"])
    assert image["pdf_sha256"] == "f" * 64
    # 只有前 top_n 条附图；其余字段与底层检索逐项一致
    assert all("page_image" not in s for s in snippets[1:])
    assert [{k: v for k, v in s.items() if k != "page_image"} for s in snippets] == plain


def test_restricted_page_and_pageless_hits_are_skipped(db, caplog):
    base, store = _base(db)
    try:
        retriever = make_page_image_retriever(
            base, "on", chunk_db_path=db, top_n=10, page_parent="dedup"
        )
        with caplog.at_level(logging.INFO):
            snippets = retriever.retrieve("Singapore VONB")
    finally:
        store.close()
    by_loc = {s.get("parent_locator") or s["source_locator"]: s for s in snippets}
    assert "page_image" in by_loc["deck.md@page=1"]
    # 第 2 页有一块 RESTRICTED：即便映射表里有图也不发
    assert "page_image" not in by_loc["deck.md@page=2"]
    assert all("page_image" not in s for s in snippets if s["doc_id"] == "legacy.pdf")
    trace = [r for r in caplog.records if getattr(r, "op", "") == "narrative.page_images"]
    assert len(trace) == 1
    rec = trace[0]
    assert rec.n_attached == 1
    assert rec.skip_reasons["restricted"] == 1
    assert rec.skip_reasons["no_page"] == 2
    assert rec.n_skipped == sum(rec.skip_reasons.values())
    # 隐私：trace 里没有路径 / 正文
    dumped = repr(rec.__dict__)
    assert ".png" not in dumped and str(db.parent) not in dumped


def test_page_parent_off_attaches_nothing(db, caplog):
    base, store = _base(db, mode="off")
    try:
        retriever = make_page_image_retriever(
            base, "on", chunk_db_path=db, top_n=3, page_parent="off"
        )
        with caplog.at_level(logging.INFO):
            snippets = retriever.retrieve("Singapore VONB")
    finally:
        store.close()
    assert snippets and all("page_image" not in s for s in snippets)
    rec = next(r for r in caplog.records if getattr(r, "op", "") == "narrative.page_images")
    assert rec.reason == "page_parent_off" and rec.n_attached == 0


def test_missing_mapping_or_file_is_counted(db, tmp_path, caplog):
    images = PageImageStore(db)
    lost = images.get("deck.md", 3)
    images.clear_doc("deck.md")
    images.replace_doc(
        "deck.md",
        pdf_sha256="f" * 64,
        pdf_pages=3,
        dpi=144,
        max_side=1568,
        signature="s2",
        pages=[RenderedPage(3, _png("deck-3"), 10, 5)],
    )
    images.close()
    assert lost is not None
    lost.path.unlink()
    base, store = _base(db)
    try:
        retriever = make_page_image_retriever(
            base, "on", chunk_db_path=db, top_n=10, page_parent="dedup"
        )
        with caplog.at_level(logging.INFO):
            snippets = retriever.retrieve("Hong Kong revenue Singapore")
    finally:
        store.close()
    assert all("page_image" not in s for s in snippets)
    rec = next(r for r in caplog.records if getattr(r, "op", "") == "narrative.page_images")
    assert rec.skip_reasons.get("missing_file") == 1
    assert rec.skip_reasons.get("no_image", 0) >= 1


def test_no_mapping_table_at_all(tmp_path, caplog):
    path = tmp_path / "knowledge.db"
    store = ChunkStore(path)
    load_page_corpus(store)
    try:
        base = NarrativeIndexRetriever(NarrativeIndex(store, page_parent="dedup"))
        retriever = make_page_image_retriever(
            base, "on", chunk_db_path=path, top_n=3, page_parent="dedup"
        )
        with caplog.at_level(logging.INFO):
            snippets = retriever.retrieve("Singapore VONB")
    finally:
        store.close()
    assert all("page_image" not in s for s in snippets)
    check = ChunkStore(path)
    try:
        tables = {
            r[0] for r in check.execute_read("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        check.close()
    assert "page_image" not in tables
