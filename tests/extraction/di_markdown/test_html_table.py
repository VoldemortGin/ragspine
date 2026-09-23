"""DI markdown 的 HTML 表格 → 矩形网格（保留合并信息）的公开契约测试。

DI（prebuilt-layout, outputContentFormat=markdown）用 HTML `<table>` 表达表格：`<th>`/`<td>`、
rowspan/colspan、`<caption>`，单元格文本经 HTML 转义。本解析器只用 stdlib HTMLParser。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.html_table import parse_html_table
from ragspine.extraction.di_markdown.models import TableCell


def test_th_and_td_are_both_cells_with_header_flag():
    grid = parse_html_table(
        "<table><tr><th>Item</th><th>2025</th></tr><tr><td>Revenue</td><td>10</td></tr></table>"
    )
    assert (grid.n_rows, grid.n_cols) == (2, 2)
    assert grid.rows == (("Item", "2025"), ("Revenue", "10"))
    assert [c.is_header for c in grid.cells] == [True, True, False, False]
    assert grid.header_row_count == 1


def test_colspan_expands_and_keeps_merge_info():
    grid = parse_html_table(
        '<table><tr><th colspan="2">Period</th><th>X</th></tr>'
        "<tr><td>a</td><td>b</td><td>c</td></tr></table>"
    )
    assert grid.rows == (("Period", "Period", "X"), ("a", "b", "c"))
    anchor = grid.cells[0]
    assert anchor == TableCell(row=0, col=0, text="Period", is_header=True, row_span=1, col_span=2)
    assert anchor.is_merged
    assert grid.anchor_at(0, 1) is anchor
    assert grid.anchor_at(0, 2).text == "X"
    assert len(grid.cells) == 5  # 被合并吞掉的位置不产出独立 cell


def test_rowspan_shifts_following_rows():
    grid = parse_html_table(
        '<table><tr><th rowspan="2">%</th><th colspan="2">H1</th></tr>'
        "<tr><th>A</th><th>B</th></tr>"
        "<tr><td>x</td><td>1</td><td>2</td></tr></table>"
    )
    assert grid.rows == (("%", "H1", "H1"), ("%", "A", "B"), ("x", "1", "2"))
    assert grid.anchor_at(1, 0) == TableCell(0, 0, "%", True, row_span=2, col_span=1)
    assert grid.header_row_count == 2


def test_rowspan_and_colspan_together():
    grid = parse_html_table(
        '<table><tr><td rowspan="2" colspan="2">M</td><td>a</td></tr>'
        "<tr><td>b</td></tr><tr><td>c</td><td>d</td><td>e</td></tr></table>"
    )
    assert grid.rows == (("M", "M", "a"), ("M", "M", "b"), ("c", "d", "e"))
    assert grid.cells[0].row_span == 2 and grid.cells[0].col_span == 2


def test_whitespace_is_collapsed_and_inline_tags_dropped():
    grid = parse_html_table(
        "<table>\n<tr>\n  <td>\n   Net   \n  profit <b>after</b><br/>tax\n</td>\n"
        "<td>&nbsp; 1 &nbsp;</td></tr>\n</table>"
    )
    assert grid.rows == (("Net profit after tax", "1"),)


def test_html_entities_are_unescaped():
    grid = parse_html_table(
        "<table><tr><td>Government &amp; Agency</td><td>&lt;1%</td>"
        "<td>&gt;30%</td><td>&#8212;</td><td>&quot;q&quot;</td></tr></table>"
    )
    assert grid.rows == (("Government & Agency", "<1%", ">30%", "—", '"q"'),)


def test_caption_is_kept_apart_from_cells():
    grid = parse_html_table(
        "<table><caption>Table 1. Demo</caption><tr><th>H</th></tr><tr><td>v</td></tr></table>"
    )
    assert grid.caption == "Table 1. Demo"
    assert grid.rows == (("H",), ("v",))


def test_thead_tbody_wrappers_are_transparent():
    grid = parse_html_table(
        "<table><thead><tr><th>H</th></tr></thead><tbody><tr><td>v</td></tr></tbody></table>"
    )
    assert grid.rows == (("H",), ("v",))


def test_ragged_rows_are_padded_to_a_rectangle():
    grid = parse_html_table("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>")
    assert grid.rows == (("a", "b"), ("c", ""))
    assert grid.anchor_at(1, 1) is None


def test_unclosed_cells_rows_and_table_are_tolerated():
    grid = parse_html_table("<table><tr><td>a<td>b<tr><td>c<td>d")
    assert grid.rows == (("a", "b"), ("c", "d"))


def test_rowspan_past_the_last_row_is_clipped():
    grid = parse_html_table('<table><tr><td rowspan="5">a</td><td>b</td></tr></table>')
    assert grid.rows == (("a", "b"),)
    assert grid.cells[0].row_span == 1


def test_bad_span_values_fall_back_to_one():
    grid = parse_html_table(
        '<table><tr><td colspan="x">a</td><td rowspan="0">b</td><td colspan="-2">c</td></tr></table>'
    )
    assert grid.rows == (("a", "b", "c"),)
    assert all(c.row_span == 1 and c.col_span == 1 for c in grid.cells)


def test_nested_table_text_folds_into_the_outer_cell():
    grid = parse_html_table(
        "<table><tr><td>outer <table><tr><td>in1</td><td>in2</td></tr></table></td>"
        "<td>z</td></tr></table>"
    )
    assert grid.rows == (("outer in1 in2", "z"),)


def test_empty_table():
    grid = parse_html_table("<table></table>")
    assert (grid.n_rows, grid.n_cols) == (0, 0)
    assert grid.rows == ()
    assert grid.cells == ()
    assert grid.header_row_count == 0
