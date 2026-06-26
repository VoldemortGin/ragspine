"""一期（Excel 线）测试共享 fixture：fixtures 路径、ground truth 加载、临时 sqlite。

6 个测试模块（ir / xlsx_styled_extractor / color_semantics / review_queue /
fact_store v2 / extraction_eval）共用这里的 pytest fixture，避免各自重复造数。
合成 fixture 缺失时自动一键再生（确定性、可重跑），不依赖任何真实敏感数据
（PRD user story 20）。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.fixtures.excel import GT_PATH, XLSX_PATH, main as make_excel_fixtures


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixtures() -> None:
    """整轮测试前确保合成 fixture 与 ground truth 存在（缺失则一键再生）。"""
    if not XLSX_PATH.exists() or not GT_PATH.exists():
        make_excel_fixtures()


@pytest.fixture(scope="session")
def fixtures_dir():
    """data/fixtures 目录路径。"""
    return GT_PATH.parent


@pytest.fixture(scope="session")
def excel_fixture_path():
    """合成 Excel fixture（excel_styled_fixture.xlsx）的绝对路径。"""
    return XLSX_PATH


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    """加载逐格真值 JSON（原始结构：file / theme_palette / sheets / cells）。"""
    return json.loads(GT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def cell_truth(ground_truth) -> dict:
    """逐格真值索引：'sheet!cell_ref' -> 期望字段 dict，便于断言取用。"""
    return {f"{c['sheet']}!{c['cell_ref']}": c for c in ground_truth["cells"]}


@pytest.fixture
def tmp_db_path(tmp_path):
    """每个测试独立的临时 sqlite 路径（fact_store / registry / queue 共用）。"""
    return tmp_path / "test.db"


@pytest.fixture
def tmp_sqlite_factory(tmp_path):
    """生成多个互不冲突的临时 sqlite 路径（同一测试需开多个库时用）。"""
    counter = {"n": 0}

    def _make(name: str = "db") -> str:
        counter["n"] += 1
        return str(tmp_path / f"{name}_{counter['n']}.db")

    return _make


# ===========================================================================
# 二期（PDF 线）fixture：合成 PDF 路径、ground truth 加载、缺失再生（autouse）。
# 仿照上方 Excel 的写法，纯追加，不改动既有内容。
# ===========================================================================

from ragspine.fixtures.pdf import (
    DIGITAL_PATH,
    GT_PATH as PDF_GT_PATH,
    MIXED_PATH,
    OCR_SCAN_PATH,
    OUT_DIR as PDF_OUT_DIR,
    PPT_EXPORT_PATH,
    SCANNED_PATH,
    main as make_pdf_fixtures,
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_pdf_fixtures() -> None:
    """整轮测试前确保 PDF 合成 fixture 与 ground truth 存在（缺失则一键再生）。"""
    paths = (DIGITAL_PATH, SCANNED_PATH, OCR_SCAN_PATH, MIXED_PATH,
             PPT_EXPORT_PATH, PDF_GT_PATH)
    if not all(p.exists() for p in paths):
        make_pdf_fixtures()


@pytest.fixture(scope="session")
def pdf_fixtures_dir():
    """data/fixtures/pdf 目录路径。"""
    return PDF_OUT_DIR


@pytest.fixture(scope="session")
def pdf_ground_truth() -> dict:
    """加载 PDF 线 ground truth（files 期望 + dual_channel 测试向量）。"""
    return json.loads(PDF_GT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def digital_pdf_path():
    """digital.pdf（数字型：表格页 + 叙述页）绝对路径。"""
    return DIGITAL_PATH


@pytest.fixture(scope="session")
def scanned_pdf_path():
    """scanned.pdf（扫描型：整页位图、无文本层）绝对路径。"""
    return SCANNED_PATH


@pytest.fixture(scope="session")
def ocr_scan_pdf_path():
    """ocr_scan.pdf（OCR 扫描型：位图 + 隐形文本层）绝对路径。"""
    return OCR_SCAN_PATH


@pytest.fixture(scope="session")
def mixed_pdf_path():
    """mixed.pdf（混合型：2 数字 + 2 扫描）绝对路径。"""
    return MIXED_PATH


@pytest.fixture(scope="session")
def ppt_export_pdf_path():
    """ppt_export.pdf（PowerPoint 导出，producer 元数据标记）绝对路径。"""
    return PPT_EXPORT_PATH


# ===========================================================================
# 三期（PPT 增强 + 扫描线）fixture：合成 pptx 路径、ground truth 加载、缺失再生
# （autouse）。仿照上方 Excel / PDF 的写法，纯追加，不改动既有内容。
# OCR fake 测试向量内嵌在 pptx ground truth 里（引用二期已生成的 scanned.pdf）。
# ===========================================================================

from ragspine.fixtures.pptx import (
    GT_PATH as PPTX_GT_PATH,
    PPTX_PATH,
    main as make_pptx_fixtures,
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_pptx_fixtures(_ensure_pdf_fixtures) -> None:
    """整轮测试前确保 pptx 合成 fixture 与 ground truth 存在（缺失则一键再生）。

    依赖 _ensure_pdf_fixtures 先跑：OCR fake 向量引用 scanned.pdf，须保证其已生成。
    """
    if not PPTX_PATH.exists() or not PPTX_GT_PATH.exists():
        make_pptx_fixtures()


@pytest.fixture(scope="session")
def pptx_fixtures_dir():
    """data/fixtures/pptx 目录路径。"""
    return PPTX_GT_PATH.parent


@pytest.fixture(scope="session")
def styled_deck_path():
    """styled_deck.pptx（增强表格 + 含数字文本框 / 备注 + 转置表）绝对路径。"""
    return PPTX_PATH


@pytest.fixture(scope="session")
def pptx_ground_truth() -> dict:
    """加载 PPT 线 ground truth（逐格值/填充、note fragments、OCR fake 向量）。"""
    return json.loads(PPTX_GT_PATH.read_text(encoding="utf-8"))


# ===========================================================================
# W3b（docspine .docx 线）fixture：纯 zipfile 合成最小 .docx 的工厂（不落二进制
# fixture、不引入 python-docx）。仿照上方各期写法，纯追加，不改动既有内容。
# docspine（纯 Rust DOCX 解析）只需最小 OOXML 包（[Content_Types].xml + _rels/.rels
# + word/document.xml）即可解析，故按需手写这三段 XML 合成（docspine CLAUDE.md 约定）。
# ===========================================================================

import zipfile
from xml.sax.saxutils import escape as _xml_escape

_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_paragraph_xml(text: str) -> str:
    if not text:
        return "<w:p/>"
    return f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'


def _docx_cell_xml(cell) -> str:
    """cell 为 str 或 {'text','gridspan','vmerge'} dict（vmerge∈{'restart','continue'}）。"""
    if isinstance(cell, dict):
        text = cell.get("text", "")
        gridspan = cell.get("gridspan")
        vmerge = cell.get("vmerge")
    else:
        text, gridspan, vmerge = cell, None, None
    props = []
    if gridspan:
        props.append(f'<w:gridSpan w:val="{int(gridspan)}"/>')
    if vmerge in ("restart", "continue"):
        # docspine 把无 val 的裸 <w:vMerge/> 视作 restart，故续格必须显式 val="continue"。
        props.append(f'<w:vMerge w:val="{vmerge}"/>')
    tcpr = f"<w:tcPr>{''.join(props)}</w:tcPr>" if props else ""
    return f"<w:tc>{tcpr}{_docx_paragraph_xml(text)}</w:tc>"


def _docx_table_xml(rows) -> str:
    trs = "".join(
        f"<w:tr>{''.join(_docx_cell_xml(c) for c in row)}</w:tr>" for row in rows
    )
    return f"<w:tbl>{trs}</w:tbl>"


def _docx_document_xml(body) -> str:
    parts = []
    for kind, payload in body:
        if kind == "para":
            parts.append(_docx_paragraph_xml(payload))
        elif kind == "table":
            parts.append(_docx_table_xml(payload))
        else:
            raise ValueError(f"unknown docx body item kind: {kind!r}")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_DOCX_W_NS}"><w:body>{"".join(parts)}</w:body></w:document>'
    )


@pytest.fixture
def make_docx():
    """合成最小 .docx 的工厂（纯 zipfile，不依赖 python-docx）。

    body 为有序列表，每项 ('para', text) 或 ('table', rows)；rows 为 list[list[cell]]，
    cell 为 str 或 {'text','gridspan','vmerge'} dict（造合并表用）。返回 build(path, body)。
    """

    def _build(path, body):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
            z.writestr("_rels/.rels", _DOCX_ROOT_RELS)
            z.writestr("word/document.xml", _docx_document_xml(body))
        return path

    return _build
