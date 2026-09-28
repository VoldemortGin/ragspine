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

from .fixtures import make_marker_md, make_md, make_pdf, write_deck


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


# ---- marker 模式（`<!-- page: N -->`，页号即真实 PDF 页码，ADR 0027）----------------------------


def _marker_deck(tmp_path, pages, pdf_pages: int, sidecar: dict | None = None):
    md = tmp_path / "deck.md"
    md.write_text(make_marker_md({n: f"body {n}" for n in pages}), encoding="utf-8")
    pdf = make_pdf(tmp_path / "deck.pdf", [f"LABEL-{i}" for i in range(1, pdf_pages + 1)])
    if sidecar is not None:
        sidecar_path(md).write_text(json.dumps(sidecar), encoding="utf-8")
    return md, pdf


def _superindex_sidecar(pages: int, **extra) -> dict:
    # SuperIndex azure_di.extract_corpus 的 sidecar 形状：pages = describe(result) 的分析页数
    base = {"source": "deck.pdf", "model": "prebuilt-layout", "page_markers": True, "pages": pages}
    return base | extra


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_marker_markdown_page_count_is_max_true_page(tmp_path):
    md, _ = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8)
    assert markdown_page_count(md) == 7


def test_marker_mode_accepts_pdf_with_more_pages_without_sidecar(tmp_path):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8)
    assert validate_source_pdf(md, pdf).page_count == 8


def test_marker_mode_accepts_equal_page_count(tmp_path):
    md, pdf = _marker_deck(tmp_path, [1, 2, 3], pdf_pages=3)
    assert validate_source_pdf(md, pdf).page_count == 3


def test_marker_mode_page_beyond_pdf_is_an_explicit_error(tmp_path):
    md, pdf = _marker_deck(tmp_path, [7, 9], pdf_pages=8)
    with pytest.raises(SourcePdfError, match="超出"):
        validate_source_pdf(md, pdf)
    # sidecar 与 md 一致也救不了超出 PDF 的页号
    sidecar_path(md).write_text(json.dumps(_superindex_sidecar(2)), encoding="utf-8")
    with pytest.raises(SourcePdfError, match="超出"):
        validate_source_pdf(md, pdf)


def test_marker_mode_sidecar_without_checked_fields_is_skipped(tmp_path):
    md, pdf = _marker_deck(tmp_path, [5, 6], pdf_pages=8, sidecar={"model": "prebuilt-layout"})
    assert validate_source_pdf(md, pdf).page_count == 8


def test_superindex_sidecar_partial_analysis_passes(tmp_path):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=20, sidecar=_superindex_sidecar(3))
    assert validate_source_pdf(md, pdf).page_count == 20


def test_superindex_sidecar_counts_distinct_marker_pages(tmp_path):
    md = tmp_path / "deck.md"
    md.write_text(make_marker_md({5: "a", 6: "b"}) + make_marker_md({5: "again"}), encoding="utf-8")
    pdf = make_pdf(tmp_path / "deck.pdf", [f"L{i}" for i in range(1, 9)])
    sidecar_path(md).write_text(json.dumps(_superindex_sidecar(2)), encoding="utf-8")
    assert validate_source_pdf(md, pdf).page_count == 8


@pytest.mark.parametrize("recorded", [2, 4, 20])
def test_superindex_sidecar_pages_must_equal_distinct_marker_pages(tmp_path, recorded):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=20, sidecar=_superindex_sidecar(recorded))
    with pytest.raises(SourcePdfError, match="sidecar"):
        validate_source_pdf(md, pdf)


@pytest.mark.parametrize(
    "key", ["source_pdf_sha256", "pdf_sha256", "source_sha256"], ids=lambda k: k
)
def test_sidecar_pdf_sha256_must_match(tmp_path, key):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8)
    sidecar_path(md).write_text(
        json.dumps(_superindex_sidecar(3, **{key: _sha(pdf).upper()})), encoding="utf-8"
    )
    assert validate_source_pdf(md, pdf).sha256 == _sha(pdf)
    sidecar_path(md).write_text(
        json.dumps(_superindex_sidecar(3, **{key: "0" * 64})), encoding="utf-8"
    )
    with pytest.raises(SourcePdfError, match="sha256"):
        validate_source_pdf(md, pdf)


def test_sidecar_without_sha256_skips_the_hash_check(tmp_path):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar=_superindex_sidecar(3))
    assert validate_source_pdf(md, pdf).page_count == 8


def test_ragspine_sidecar_page_count_is_pdf_total_and_must_equal(tmp_path):
    # ragspine pdf_to_di_markdown 的 sidecar：page_count = 原 PDF 总页数，pages 是逐页列表
    sidecar = {"generator": "pdf_to_di_markdown.py", "page_count": 8, "pages": [{"page": 5}]}
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar=sidecar)
    assert validate_source_pdf(md, pdf).page_count == 8
    for wrong in (3, 7):  # 3 = 标记页数：总页数不享受部分分析的放宽
        sidecar_path(md).write_text(json.dumps({**sidecar, "page_count": wrong}), encoding="utf-8")
        with pytest.raises(SourcePdfError, match="sidecar"):
            validate_source_pdf(md, pdf)


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'], ids=["broken", "list", "string"])
def test_marker_mode_corrupt_sidecar_is_an_error(tmp_path, raw):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8)
    sidecar_path(md).write_text(raw, encoding="utf-8")
    with pytest.raises(SourcePdfError, match="sidecar"):
        validate_source_pdf(md, pdf)


@pytest.mark.parametrize(
    "field",
    [
        {"page_count": "8"},
        {"page_count": True},
        {"page_count": None},
        {"page_count": 8.0},
        {"pages": "3"},
        {"pages": 3.0},
        {"pages": False},
        {"pages": {"n": 3}},
        {"source_pdf_sha256": 123},
        {"pdf_sha256": None},
        {"source_sha256": ["abc"]},
    ],
    ids=lambda f: f"{next(iter(f))}={next(iter(f.values()))!r}",
)
def test_marker_mode_sidecar_field_of_wrong_type_is_an_error(tmp_path, field):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar=field)
    with pytest.raises(SourcePdfError, match="sidecar"):
        validate_source_pdf(md, pdf)


@pytest.mark.parametrize("key", ["source_pdf_sha256", "pdf_sha256", "source_sha256"])
@pytest.mark.parametrize("value", ["", "   "])
def test_marker_mode_empty_sha256_is_reported_as_empty(tmp_path, key, value):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar={key: value})
    with pytest.raises(SourcePdfError, match="为空"):
        validate_source_pdf(md, pdf)


@pytest.mark.parametrize("pages", [0, -3])
def test_marker_mode_non_positive_analyzed_pages_is_an_error(tmp_path, pages):
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar=_superindex_sidecar(pages))
    with pytest.raises(SourcePdfError, match="正整数"):
        validate_source_pdf(md, pdf)


def test_ragspine_pages_list_is_not_an_analyzed_count(tmp_path):
    sidecar = {"generator": "pdf_to_di_markdown.py", "pages": [{"page": 5}, {"page": 6}]}
    md, pdf = _marker_deck(tmp_path, [5, 6, 7], pdf_pages=8, sidecar=sidecar)
    assert validate_source_pdf(md, pdf).page_count == 8


def test_page_break_mode_ignores_a_corrupt_sidecar(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    sidecar_path(md).write_text("{not json", encoding="utf-8")
    assert validate_source_pdf(md, pdf).page_count == 2


def test_page_break_mode_still_requires_equal_count(tmp_path):
    md, pdf = write_deck(tmp_path, ["a", "b"])
    more = make_pdf(tmp_path / "more.pdf", ["x", "y", "z"])
    with pytest.raises(SourcePdfError, match="页数不一致"):
        validate_source_pdf(md, more)
    # sidecar 的各字段不影响 PageBreak 模式：仍只按相等校验
    sidecar_path(md).write_text(
        json.dumps({"page_count": 3, "pages": 1, "source_pdf_sha256": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(SourcePdfError, match="页数不一致"):
        validate_source_pdf(md, more)
    assert validate_source_pdf(md, pdf).page_count == 2


# ---- 与 doc_id 冲突规则的交叉（ADR 0027 × 同名拒绝）--------------------------------------------


def _marker_deck_in(folder, pages, sidecar):
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / "deck.md"
    md.write_text(make_marker_md({n: f"{folder.name} body {n}" for n in pages}), encoding="utf-8")
    make_pdf(folder / "deck.pdf", [f"LABEL-{i}" for i in range(1, 9)])
    sidecar_path(md).write_text(
        json.dumps({SIDECAR_SOURCE_PDF_KEY: "deck.pdf", **sidecar}), encoding="utf-8"
    )
    return md


def test_same_named_marker_markdown_is_refused_before_marker_checks(tmp_path):
    bad = _marker_deck_in(
        tmp_path / "a", [5, 6, 7], {"pages": 99}
    )  # 单独校验会因 sidecar 不一致失败
    good = _marker_deck_in(tmp_path / "b", [5, 6, 7], {"pages": 3})
    # 同一批两个同名 .md 都关联 PDF：同名规则先报错（在逐个 validate_source_pdf 之前）
    with pytest.raises(SourcePdfError, match="同名"):
        prepare_source_pdfs([bad, good])
    # 各自单独入库时，marker 模式的规则照常生效
    with pytest.raises(SourcePdfError, match="sidecar"):
        prepare_source_pdfs([bad])
    assert prepare_source_pdfs([good])["deck.md"].page_count == 8


def test_same_named_marker_markdown_across_batches_hits_the_doc_id_conflict(tmp_path):
    from ragspine.ingestion.narrative.narrative_ingest import (
        STATUS_FAILED,
        STATUS_INGESTED,
        ingest_narrative,
    )
    from ragspine.retrieval.chunking.chunk_store import ChunkStore

    first = _marker_deck_in(tmp_path / "a", [5, 6, 7], {"pages": 3})
    second = _marker_deck_in(tmp_path / "b", [5, 6], {"pages": 2})
    store = ChunkStore(tmp_path / "chunks.db")
    store.init_schema()
    try:
        assert prepare_source_pdfs([first])["deck.md"].page_count == 8
        assert ingest_narrative([first], store).files[0].status == STATUS_INGESTED
        locators = [c.source_locator for c in store.iter_chunks(doc_id="deck.md")]
        # 第二批单独校验通过（marker 规则），但 doc_id 已属于 a/deck.md：拒绝而不是覆盖
        assert prepare_source_pdfs([second])["deck.md"].page_count == 8
        assert ingest_narrative([second], store).files[0].status == STATUS_FAILED
        assert [c.source_locator for c in store.iter_chunks(doc_id="deck.md")] == locators
    finally:
        store.close()
    assert locators[0].startswith("deck.md@page=5#")
