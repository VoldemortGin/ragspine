"""真实样本验收：71 页 DI 风格 markdown（pdfspine 生成的 DI 替身，见同名 .meta.json）。

样本在 data/ 下且被 gitignore；不存在时 skip，不做任何下载。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.models import Table
from ragspine.extraction.di_markdown.parse import parse_di_markdown

SAMPLE = ROOT_DIR / "data" / "di-markdown" / "aia-group-2026-interim-results-presentation.md"


@pytest.fixture(scope="module")
def doc():
    if not SAMPLE.is_file():
        pytest.skip(
            "Optional DI markdown sample is absent (data/ is git-ignored); "
            "no download is performed."
        )
    return parse_di_markdown(SAMPLE.read_text(encoding="utf-8"))


def _tables(page):
    return [b for b in page.blocks if isinstance(b, Table)]


def test_page_and_table_counts(doc):
    assert len(doc.pages) == 71
    assert [p.index for p in doc.pages] == list(range(1, 72))
    assert sum(len(_tables(p)) for p in doc.pages) == 18


def test_page_numbers_follow_comments_or_physical_order(doc):
    # 样本的 PageNumber 注释都等于物理页号；缺注释的页回落到物理页序。
    assert [p.number for p in doc.pages] == list(range(1, 72))
    assert doc.pages[1].page_number_label == "2"
    assert doc.pages[0].page_number_label is None


def test_page_metadata_is_not_body_text(doc):
    for page in doc.pages:
        for block in page.blocks:
            text = getattr(block, "text", "")
            assert "<!--" not in text and "PageHeader" not in text and "PageNumber" not in text
    assert sum(len(p.headers) for p in doc.pages) == 13


def test_page_32_six_segment_tables(doc):
    page = doc.pages[31]
    assert page.number == 32
    tables = _tables(page)
    assert len(tables) == 6
    assert [t.grid.rows[0][0] for t in tables] == [
        "Singapore ($m)",
        "Hong Kong ($m)",
        "Chinese Mainland ($m)",
        "Malaysia ($m)",
        "Thailand ($m)",
        "Other Markets ($m)",
    ]
    assert tables[0].grid.rows == (
        ("Singapore ($m)", "1H26", "1H25", "CER", "AER"),
        ("VONB", "294", "259", "+10%", "+14%"),
        ("VONB Margin", "45.6%", "47.4%", "(1.7) pps", "(1.8) pps"),
        ("ANP", "644", "547", "+14%", "+18%"),
        ("TWPI", "3,170", "2,616", "+17%", "+21%"),
        ("OPAT", "411", "355", "+10%", "+16%"),
    )
    assert tables[5].grid.rows[-1] == ("OPAT", "356", "338", "+8%", "+5%")
    assert all(t.grid.header_row_count == 1 for t in tables)
    assert all(t.heading_path == ("Geographical Market Performance",) for t in tables)


def test_page_43_merged_header_table(doc):
    page = doc.pages[42]
    assert page.number == 43
    (table,) = _tables(page)
    grid = table.grid
    assert (grid.n_rows, grid.n_cols) == (16, 7)
    assert grid.header_row_count == 2
    head = ("Risk Discount Rates", "Long-term 10-year Govt Bonds", "Risk Premium")
    assert grid.rows[0] == ("%",) + ("As at 30 Nov 2010",) * 3 + ("As at 30 Jun 2026",) * 3
    assert grid.rows[1] == ("%",) + head + head
    assert grid.rows[2] == ("Australia", "8.75", "5.65", "3.10", "7.43", "3.80", "3.63")
    assert grid.rows[11] == ("Sri Lanka(1)", "n/a", "n/a", "n/a", "14.70", "10.00", "4.70")
    assert grid.rows[15] == ("Weighted Average(2)", "8.95", "3.85", "5.10", "7.84", "3.28", "4.56")
    merged = [(c.row, c.col, c.row_span, c.col_span) for c in grid.cells if c.is_merged]
    assert merged == [(0, 0, 2, 1), (0, 1, 1, 3), (0, 4, 1, 3)]
