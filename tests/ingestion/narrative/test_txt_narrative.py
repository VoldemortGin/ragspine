"""纯文本（.txt）叙事抽取红色测试：空行分段 -> NarrativeDoc segments；locator='para={N}'。

只验证对外行为：就地写临时 .txt，断言 extract_txt_narrative / extract_narrative 的
segments / locator / 归一化 / doc 字段，端到端 ingest_narrative 切块入库，及确定性。

红色预期：`extract_txt_narrative` / SUPPORTED_SUFFIXES 含 .txt 尚未实现 -> FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.narrative.narrative_extract import (
    NarrativeDoc,
    SUPPORTED_SUFFIXES,
    extract_narrative,
    extract_txt_narrative,
)


def test_txt_blocks_become_segments(tmp_path):
    """两个空行分隔的段落块 -> 两段，locator='para=1'/'para=2'，内部空白折叠归一化。"""
    p = tmp_path / "memo.txt"
    p.write_text(
        "FY2024 Hong Kong performance   review\n\n"
        "REVENUE grew strongly in 2024.",
        encoding="utf-8",
    )
    doc = extract_txt_narrative(p)
    assert isinstance(doc, NarrativeDoc)
    assert [s.source_locator for s in doc.segments] == ["para=1", "para=2"]
    assert doc.segments[0].text == "FY2024 Hong Kong performance review"
    assert doc.segments[1].text == "REVENUE grew strongly in 2024."
    assert doc.doc_id == "memo.txt"
    assert doc.file_hash
    assert doc.skipped_pages == 0


def test_txt_extra_blank_lines_dont_create_or_skip_segments(tmp_path):
    """首/尾/块间多余空行不产空段、不跳序号（与 docx 空段不占序同口径）。"""
    p = tmp_path / "memo.txt"
    p.write_text(
        "\n\n  \nFirst block.\n\n\n\nSecond block.\n \n\n",
        encoding="utf-8",
    )
    doc = extract_txt_narrative(p)
    assert [s.source_locator for s in doc.segments] == ["para=1", "para=2"]
    assert [s.text for s in doc.segments] == ["First block.", "Second block."]


def test_extract_narrative_dispatches_txt(tmp_path):
    """extract_narrative 按后缀把 .txt 分发到 txt 抽取器；SUPPORTED_SUFFIXES 含 .txt。"""
    assert ".txt" in SUPPORTED_SUFFIXES
    p = tmp_path / "notes.txt"
    p.write_text("Hello world.", encoding="utf-8")
    doc = extract_narrative(p)
    assert isinstance(doc, NarrativeDoc)
    assert [s.text for s in doc.segments] == ["Hello world."]


def test_extract_narrative_unsupported_mentions_txt(tmp_path):
    """不支持的后缀仍 ValueError，且提示信息把 .txt 列进受支持类型。"""
    p = tmp_path / "notes.rtf"
    p.write_text("plain text", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.txt"):
        extract_narrative(p)


def test_txt_extraction_is_deterministic(tmp_path):
    """同一 .txt 两次抽取逐字段一致（确定性）。"""
    p = tmp_path / "memo.txt"
    p.write_text("Block one.\n\nBlock two.\n\nBlock three.", encoding="utf-8")
    a = extract_txt_narrative(p)
    b = extract_txt_narrative(p)
    assert a.doc_id == b.doc_id
    assert a.file_hash == b.file_hash
    assert a.skipped_pages == b.skipped_pages
    assert [(s.text, s.source_locator) for s in a.segments] == [
        (s.text, s.source_locator) for s in b.segments
    ]


def test_txt_ingests_into_chunks(tmp_path, tmp_db_path):
    """端到端：目录中的 .txt 经默认 ingest_narrative 切块入库，可被检索，血缘用 .txt 文件名。"""
    from ragspine.ingestion.narrative.narrative_ingest import (
        STATUS_INGESTED,
        _resolve_inputs,
        ingest_narrative,
    )
    from ragspine.retrieval.chunking.chunk_store import ChunkStore

    folder = tmp_path / "docs"
    folder.mkdir()
    txt = folder / "townhall_FY2024.txt"
    txt.write_text(
        "Agency expansion remains on track.\n\n"
        "Regulatory outlook remains stable for the quarter ahead.",
        encoding="utf-8",
    )
    # folder-scan 收进 .txt
    assert txt in _resolve_inputs(folder)

    store = ChunkStore(tmp_db_path)
    store.init_schema()
    try:
        report = ingest_narrative(folder, store)
        by_id = {f.doc_id: f for f in report.files}
        f = by_id["townhall_FY2024.txt"]
        assert f.status == STATUS_INGESTED
        assert f.n_chunks == store.count() > 0
        rows = store.iter_chunks(doc_id="townhall_FY2024.txt")
        assert rows and all(r.doc_id == "townhall_FY2024.txt" for r in rows)
    finally:
        store.close()
