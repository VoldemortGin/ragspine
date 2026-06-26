"""docx 叙事抽取（W3b）红色测试：正文段落 -> NarrativeDoc segments；表格内容跳过。

只验证对外行为：就地（纯 zipfile）合成最小 .docx，断言 extract_docx_narrative /
extract_narrative 的 segments / locator / to_text，以及端到端 ingest_narrative 切块入库。

红色预期：`extract_docx_narrative` / SUPPORTED_SUFFIXES 含 .docx 尚未实现 -> FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

docspine = pytest.importorskip("docspine", reason="docspine 未安装（[doc]）")

TABLE_MARKER = "TABLE_CELL_SENTINEL"


def test_docx_paragraphs_become_segments(make_docx, tmp_path):
    """user story：.docx 正文段落按文档顺序成叙事段，locator='para={N}'（非空段序）。"""
    from ragspine.ingestion.narrative.narrative_extract import extract_docx_narrative

    p = tmp_path / "memo.docx"
    make_docx(p, [
        ("para", "FY2024 Hong Kong performance review"),
        ("table", [[TABLE_MARKER, "FY2024"], ["REVENUE", "2680"]]),
        ("para", "Loss attribution pending review."),
    ])
    doc = extract_docx_narrative(p)
    assert doc.doc_id == "memo.docx"
    assert doc.file_hash
    assert [s.source_locator for s in doc.segments] == ["para=1", "para=2"]
    assert doc.segments[0].text == "FY2024 Hong Kong performance review"
    assert doc.segments[1].text == "Loss attribution pending review."


def test_docx_table_content_skipped(make_docx, tmp_path):
    """user story：表格单元格文本不进叙事通路（表格数字归结构化通路，与 pptx 同口径）。"""
    from ragspine.ingestion.narrative.narrative_extract import extract_docx_narrative

    p = tmp_path / "memo.docx"
    make_docx(p, [
        ("para", "Intro paragraph."),
        ("table", [[TABLE_MARKER, "x"]]),
    ])
    doc = extract_docx_narrative(p)
    assert TABLE_MARKER not in doc.to_text()


def test_docx_empty_paragraphs_not_numbered(make_docx, tmp_path):
    """user story：空段落不占 para 序号（与 pptx 仅给非空 frame 编号同口径）。"""
    from ragspine.ingestion.narrative.narrative_extract import extract_docx_narrative

    p = tmp_path / "memo.docx"
    make_docx(p, [("para", "First."), ("para", ""), ("para", "Second.")])
    doc = extract_docx_narrative(p)
    assert [s.source_locator for s in doc.segments] == ["para=1", "para=2"]
    assert [s.text for s in doc.segments] == ["First.", "Second."]


def test_extract_narrative_dispatches_docx(make_docx, tmp_path):
    """user story：extract_narrative 按后缀把 .docx 分发到 docx 抽取器；folder-scan 也收 .docx。"""
    from ragspine.ingestion.narrative.narrative_extract import (
        SUPPORTED_SUFFIXES,
        extract_narrative,
    )

    assert ".docx" in SUPPORTED_SUFFIXES
    p = tmp_path / "memo.docx"
    make_docx(p, [("para", "Hello world.")])
    doc = extract_narrative(p)
    assert [s.text for s in doc.segments] == ["Hello world."]


def test_docx_ingests_into_chunks(make_docx, tmp_path, tmp_db_path):
    """user story：含表格 + 段落的 .docx 经默认 ingest_narrative -> 段落成 chunks 入库
    （表格数字另走结构化通路），Word 来源的叙事文本可被检索。"""
    from ragspine.ingestion.narrative.narrative_ingest import (
        STATUS_INGESTED,
        ingest_narrative,
    )
    from ragspine.retrieval.chunking.chunk_store import ChunkStore

    p = tmp_path / "qbr_FY2024.docx"
    make_docx(p, [
        ("para", "FY2024 Hong Kong performance review and loss attribution."),
        ("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]]),
        ("para", "Closing narrative remarks for the quarter ahead."),
    ])
    store = ChunkStore(tmp_db_path)
    store.init_schema()
    try:
        report = ingest_narrative([p], store)
        f = report.files[0]
        assert f.status == STATUS_INGESTED
        assert f.n_chunks == store.count() > 0
        # 表格数字不进叙事通路
        texts = " ".join(c.text for c in store.iter_chunks(doc_id="qbr_FY2024.docx"))
        assert "2680" not in texts
    finally:
        store.close()
