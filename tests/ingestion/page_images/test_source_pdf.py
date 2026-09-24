"""DI markdown ↔ 原 PDF 的显式关联：显式参数 / sidecar 字段解析，页数校验与 sha256。"""

import hashlib
import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.page_images.source_pdf import (
    SIDECAR_SOURCE_PDF_KEY,
    SourcePdfError,
    markdown_page_count,
    prepare_source_pdfs,
    resolve_source_pdf,
    sidecar_path,
    validate_source_pdf,
)

from .fixtures import make_md, make_pdf, write_deck


def test_sidecar_field_name_and_path(tmp_path):
    assert SIDECAR_SOURCE_PDF_KEY == "source_pdf"
    assert sidecar_path(tmp_path / "deck.md") == tmp_path / "deck.meta.json"


def test_markdown_page_count_is_max_physical_index(tmp_path):
    md = tmp_path / "deck.md"
    md.write_text(make_md(["a", "b", "c"]), encoding="utf-8")
    assert markdown_page_count(md) == 3


def test_no_explicit_and_no_sidecar_means_no_pdf(tmp_path):
    md, _ = write_deck(tmp_path, ["a"])
    assert resolve_source_pdf(md) is None
    assert prepare_source_pdfs([md]) == {}


def test_explicit_pdf_wins(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    assert resolve_source_pdf(md, pdf) == pdf.resolve()


def test_sidecar_relative_to_sidecar_dir(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    sidecar_path(md).write_text(json.dumps({SIDECAR_SOURCE_PDF_KEY: pdf.name}), encoding="utf-8")
    assert resolve_source_pdf(md) == pdf.resolve()


def test_sidecar_relative_to_cwd_fallback(tmp_path, monkeypatch):
    md, pdf = write_deck(tmp_path, ["a"])
    sub = tmp_path / "md"
    sub.mkdir()
    moved = sub / md.name
    md.rename(moved)
    sidecar_path(moved).write_text(
        json.dumps({SIDECAR_SOURCE_PDF_KEY: pdf.name, "other": 1}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert resolve_source_pdf(moved) == pdf.resolve()


def test_sidecar_without_field_means_no_pdf(tmp_path):
    md, _ = write_deck(tmp_path, ["a"])
    sidecar_path(md).write_text(json.dumps({"page_count": 1}), encoding="utf-8")
    assert resolve_source_pdf(md) is None


def test_missing_pdf_is_an_error(tmp_path):
    md, _ = write_deck(tmp_path, ["a"])
    with pytest.raises(SourcePdfError, match="不存在"):
        resolve_source_pdf(md, tmp_path / "nope.pdf")
    sidecar_path(md).write_text(json.dumps({SIDECAR_SOURCE_PDF_KEY: "gone.pdf"}), encoding="utf-8")
    with pytest.raises(SourcePdfError, match="不存在"):
        resolve_source_pdf(md)


def test_validate_records_sha_and_page_count(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    src = validate_source_pdf(md, pdf)
    assert src.page_count == 2
    assert src.sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert src.path == pdf.resolve()


def test_page_count_mismatch_is_an_error(tmp_path):
    md, _ = write_deck(tmp_path, ["a", "b", "c"])
    wrong = make_pdf(tmp_path / "wrong.pdf", ["x", "y"])
    with pytest.raises(SourcePdfError, match="页数不一致"):
        validate_source_pdf(md, wrong)


def test_not_a_pdf_is_an_error(tmp_path):
    md, _ = write_deck(tmp_path, ["a"])
    bogus = tmp_path / "bogus.pdf"
    bogus.write_bytes(b"not a pdf")
    with pytest.raises(SourcePdfError):
        validate_source_pdf(md, bogus)


def test_prepare_keys_by_doc_id(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    sources = prepare_source_pdfs([md], pdf)
    assert list(sources) == ["deck.md"]
    assert sources["deck.md"].page_count == 2


def test_explicit_pdf_needs_exactly_one_markdown(tmp_path):
    md, pdf = write_deck(tmp_path, ["a"])
    other = tmp_path / "other.md"
    other.write_text(make_md(["z"]), encoding="utf-8")
    with pytest.raises(SourcePdfError, match="一个"):
        prepare_source_pdfs([md, other], pdf)
    with pytest.raises(SourcePdfError, match="一个"):
        prepare_source_pdfs([], pdf)


def test_non_markdown_inputs_are_ignored(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello", encoding="utf-8")
    assert prepare_source_pdfs([txt]) == {}


def test_allowed_root_applies_to_pdf(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    md, _ = write_deck(inside, ["a"])
    outside = make_pdf(tmp_path / "outside.pdf", ["a"])
    with pytest.raises(SourcePdfError, match="allowed"):
        prepare_source_pdfs([md], outside, allowed_root=inside)
