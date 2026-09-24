"""页图渲染（pdfspine）与入库同步：内容寻址落盘、映射表、幂等、PDF 变更替换、RESTRICTED 页不渲染。"""

import os
import struct

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.page_images.index import sync_page_images
from ragspine.ingestion.page_images.render import (
    DEFAULT_PAGE_IMAGE_DPI,
    DEFAULT_PAGE_IMAGE_MAX_SIDE,
    render_pdf_pages,
)
from ragspine.ingestion.page_images.source_pdf import validate_source_pdf
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.page_images.store import PageImageStore, default_page_image_dir

from .fixtures import make_pdf, write_deck


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


def test_defaults():
    assert DEFAULT_PAGE_IMAGE_DPI == 144
    assert DEFAULT_PAGE_IMAGE_MAX_SIDE == 1568


def test_render_respects_dpi_and_max_side(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["one", "two"], width=400, height=225)
    pages = render_pdf_pages(pdf, dpi=144, max_side=10_000)
    assert [p.page for p in pages] == [1, 2]
    assert (pages[0].width, pages[0].height) == (800, 450)
    assert _png_size(pages[0].png) == (800, 450)
    capped = render_pdf_pages(pdf, dpi=144, max_side=400)
    assert max(capped[0].width, capped[0].height) <= 400
    assert capped[0].width == 400


def test_render_selected_pages_and_determinism(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["one", "two", "three"])
    only = render_pdf_pages(pdf, dpi=72, max_side=2000, pages=[3, 1])
    assert [p.page for p in only] == [1, 3]
    again = render_pdf_pages(pdf, dpi=72, max_side=2000, pages=[1, 3])
    assert [p.png for p in only] == [p.png for p in again]


def _ingest_chunks(db, doc_id: str, n_pages: int, restricted: set[int] = frozenset()) -> None:
    store = ChunkStore(db)
    store.init_schema()
    store.replace_doc_chunks(
        doc_id,
        [
            Chunk(
                chunk_id=f"{doc_id}#c{p}",
                doc_id=doc_id,
                seq=p,
                text=f"page {p}",
                source_locator=f"{doc_id}@page={p}#para1",
                para_start=1,
                para_end=1,
                sensitivity="RESTRICTED" if p in restricted else "INTERNAL",
            )
            for p in range(1, n_pages + 1)
        ],
    )
    store.close()


def _rows(db, image_dir):
    store = PageImageStore(db, image_dir)
    try:
        return store.list_doc("deck.md")
    finally:
        store.close()


def test_sync_renders_and_maps_every_page(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a", "b", "c"])
    _ingest_chunks(db, "deck.md", 3)
    src = validate_source_pdf(md, pdf)

    report = sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)

    assert report.counts() == {"rendered": 1, "unchanged": 0, "cleared": 0, "dry_run": 0}
    doc = report.docs[0]
    assert (doc.n_pages, doc.n_images, doc.n_withheld) == (3, 3, 0)
    image_dir = default_page_image_dir(db)
    assert image_dir == tmp_path / "page_images"
    rows = _rows(db, image_dir)
    assert [r.page for r in rows] == [1, 2, 3]
    for r in rows:
        assert r.doc_id == "deck.md"
        assert r.pdf_sha256 == src.sha256
        assert r.dpi == 72
        assert r.path.is_file()
        # 内容寻址：文件名就是 PNG 内容的 sha256
        assert r.path.name == f"{r.image_sha256}.png"
        assert r.path.parent.name == r.image_sha256[:2]
        assert r.path.is_relative_to(image_dir)


def test_sync_is_idempotent(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a", "b"])
    _ingest_chunks(db, "deck.md", 2)
    src = validate_source_pdf(md, pdf)
    sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)
    before = [(r.page, r.image_sha256) for r in _rows(db, default_page_image_dir(db))]

    again = sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)

    assert again.docs[0].status == "unchanged"
    assert [(r.page, r.image_sha256) for r in _rows(db, default_page_image_dir(db))] == before


def test_pdf_change_replaces_images_and_removes_orphans(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a", "b"], labels=["OLD-1", "OLD-2"])
    _ingest_chunks(db, "deck.md", 2)
    sync_page_images(db, {"deck.md": validate_source_pdf(md, pdf)}, dpi=72, max_side=1568)
    old_files = [r.path for r in _rows(db, default_page_image_dir(db))]

    make_pdf(pdf, ["NEW-1", "NEW-2"])
    src = validate_source_pdf(md, pdf)
    report = sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)

    assert report.docs[0].status == "rendered"
    rows = _rows(db, default_page_image_dir(db))
    assert {r.pdf_sha256 for r in rows} == {src.sha256}
    assert all(not p.exists() for p in old_files)
    assert all(r.path.is_file() for r in rows)


def test_dpi_change_rerenders(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a"])
    _ingest_chunks(db, "deck.md", 1)
    src = validate_source_pdf(md, pdf)
    sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)
    report = sync_page_images(db, {"deck.md": src}, dpi=144, max_side=1568)
    assert report.docs[0].status == "rendered"
    assert _rows(db, default_page_image_dir(db))[0].dpi == 144


def test_restricted_pages_are_never_rendered(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a", "b", "c"])
    _ingest_chunks(db, "deck.md", 3, restricted={2})
    report = sync_page_images(db, {"deck.md": validate_source_pdf(md, pdf)}, dpi=72, max_side=1568)
    assert (report.docs[0].n_images, report.docs[0].n_withheld) == (2, 1)
    assert [r.page for r in _rows(db, default_page_image_dir(db))] == [1, 3]


def test_sensitivity_change_resyncs(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a", "b"])
    _ingest_chunks(db, "deck.md", 2)
    src = validate_source_pdf(md, pdf)
    sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)
    _ingest_chunks(db, "deck.md", 2, restricted={1})
    report = sync_page_images(db, {"deck.md": src}, dpi=72, max_side=1568)
    assert report.docs[0].status == "rendered"
    assert [r.page for r in _rows(db, default_page_image_dir(db))] == [2]


def test_no_pdf_clears_previous_association(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a"])
    _ingest_chunks(db, "deck.md", 1)
    sync_page_images(db, {"deck.md": validate_source_pdf(md, pdf)}, dpi=72, max_side=1568)
    files = [r.path for r in _rows(db, default_page_image_dir(db))]

    report = sync_page_images(db, {"deck.md": None}, dpi=72, max_side=1568)

    assert report.counts()["cleared"] == 1
    assert _rows(db, default_page_image_dir(db)) == []
    assert all(not p.exists() for p in files)


def test_no_pdf_on_fresh_db_touches_nothing(tmp_path):
    db = tmp_path / "knowledge.db"
    _ingest_chunks(db, "deck.md", 1)
    report = sync_page_images(db, {"deck.md": None}, dpi=72, max_side=1568)
    assert report.docs == []
    store = ChunkStore(db)
    try:
        tables = {r[0] for r in store.execute_read("SELECT name FROM sqlite_master")}
    finally:
        store.close()
    assert not any(t.startswith("page_image") for t in tables)
    assert not (tmp_path / "page_images").exists()


def test_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a"])
    _ingest_chunks(db, "deck.md", 1)
    report = sync_page_images(
        db, {"deck.md": validate_source_pdf(md, pdf)}, dpi=72, max_side=1568, dry_run=True
    )
    assert report.counts()["dry_run"] == 1
    assert not (tmp_path / "page_images").exists()


def test_same_image_shared_across_docs_survives_one_clear(tmp_path):
    db = tmp_path / "knowledge.db"
    md, pdf = write_deck(tmp_path, ["a"])
    _ingest_chunks(db, "deck.md", 1)
    _ingest_chunks(db, "copy.md", 1)
    src = validate_source_pdf(md, pdf)
    sync_page_images(db, {"deck.md": src, "copy.md": src}, dpi=72, max_side=1568)
    shared = _rows(db, default_page_image_dir(db))[0].path
    sync_page_images(db, {"deck.md": None}, dpi=72, max_side=1568)
    assert shared.is_file(), "copy.md 仍引用同一内容寻址文件，不能被删"


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_render_params(tmp_path, bad):
    pdf = make_pdf(tmp_path / "a.pdf", ["x"])
    with pytest.raises(ValueError):
        render_pdf_pages(pdf, dpi=bad, max_side=100)
    with pytest.raises(ValueError):
        render_pdf_pages(pdf, dpi=72, max_side=bad)
