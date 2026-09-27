"""DI markdown → 类型化中间表示（页 → 块）的公开契约测试。

格式依据：Microsoft Learn「Document Intelligence supported Markdown elements」
（prebuilt-layout, outputContentFormat=markdown）——`<!-- PageBreak -->` 分页，
`<!-- PageHeader/PageFooter/PageNumber="..." -->` 为页元数据（不进正文），`#`–`######` 标题，
空行分段，HTML `<table>`，`<figure>`（可含 `<figcaption>`）。
"""

import ast
import os
import sys
import time

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.models import Figure, Heading, Paragraph, Table
from ragspine.extraction.di_markdown.parse import (
    MAX_MARKER_PAGE,
    _is_value_like,
    has_page_markers,
    page_marker_numbers,
    parse_di_markdown,
)


def _texts(page):
    return [(type(b).__name__, getattr(b, "text", None)) for b in page.blocks]


# ---- 分页与页码 ---------------------------------------------------------------------------


def test_page_break_splits_pages_and_physical_index_starts_at_one():
    doc = parse_di_markdown("one\n\n<!-- PageBreak -->\n\ntwo\n<!-- PageBreak -->\nthree\n")
    assert [p.index for p in doc.pages] == [1, 2, 3]
    assert [_texts(p) for p in doc.pages] == [
        [("Paragraph", "one")],
        [("Paragraph", "two")],
        [("Paragraph", "three")],
    ]


def test_page_number_comment_wins_over_physical_order():
    doc = parse_di_markdown(
        'a\n\n<!-- PageNumber="7" -->\n\n<!-- PageBreak -->\n\nb\n\n'
        '<!-- PageBreak -->\n\n<!-- PageFooter="f" -->\n<!-- PageNumber="Page 9" -->\n'
    )
    assert [(p.index, p.number, p.page_number_label) for p in doc.pages] == [
        (1, 7, "7"),
        (2, 2, None),  # 无 PageNumber → 物理页序
        (3, 9, "Page 9"),
    ]


def test_unparseable_page_number_falls_back_to_physical_order():
    doc = parse_di_markdown(
        'a\n<!-- PageNumber="iv" -->\n<!-- PageBreak -->\nb\n<!-- PageNumber="3 of 10" -->'
    )
    assert [(p.number, p.page_number_label) for p in doc.pages] == [(1, "iv"), (2, "3 of 10")]


def test_page_metadata_comments_never_reach_the_body():
    doc = parse_di_markdown(
        '<!-- PageHeader="Annual Report" -->\n\n# Title\n\nBody text.\n\n'
        '<!-- PageFooter="Confidential" -->\n<!-- PageNumber="12" -->\n'
    )
    page = doc.pages[0]
    assert _texts(page) == [("Heading", "Title"), ("Paragraph", "Body text.")]
    assert page.headers == ("Annual Report",)
    assert page.footers == ("Confidential",)
    assert page.number == 12
    joined = " ".join(t for _, t in _texts(page))
    assert "Annual Report" not in joined and "Confidential" not in joined and "12" not in joined


def test_metadata_comment_inside_a_paragraph_is_removed_without_splitting_it():
    doc = parse_di_markdown('line one\n<!-- PageHeader="H" -->\nline two\n')
    assert _texts(doc.pages[0]) == [("Paragraph", "line one\nline two")]
    assert doc.pages[0].headers == ("H",)


def test_other_comments_are_dropped_from_the_body():
    doc = parse_di_markdown("<!-- anything else -->\ntext <!-- inline --> here\n")
    assert _texts(doc.pages[0]) == [("Paragraph", "text  here")]


# ---- 空页 ---------------------------------------------------------------------------------


def test_empty_pages_are_kept_with_no_blocks():
    doc = parse_di_markdown(
        'a\n<!-- PageBreak -->\n\n<!-- PageNumber="2" -->\n<!-- PageBreak -->\n<!-- PageBreak -->\nb'
    )
    assert len(doc.pages) == 4
    assert [len(p.blocks) for p in doc.pages] == [1, 0, 0, 1]
    assert doc.pages[1].number == 2


def test_empty_input_is_one_empty_page():
    doc = parse_di_markdown("")
    assert len(doc.pages) == 1
    assert doc.pages[0].blocks == ()
    assert doc.pages[0].number == 1


# ---- 段落 ---------------------------------------------------------------------------------


def test_paragraphs_split_on_blank_lines_and_keep_inner_line_breaks():
    doc = parse_di_markdown("This is p1.\nStill p1.\n\n\nThis is p2 &amp; more &gt;40%.\n")
    assert _texts(doc.pages[0]) == [
        ("Paragraph", "This is p1.\nStill p1."),
        ("Paragraph", "This is p2 & more >40%."),
    ]


# ---- 标题栈 -------------------------------------------------------------------------------


def test_heading_stack_builds_heading_paths():
    doc = parse_di_markdown(
        "# Title\n\nintro\n\n## Section A\n\n### Sub A1\n\na1 text\n\n## Section B\n\nb text\n\n"
        "#### Deep\n\nd\n\n# Next Title\n\nn\n"
    )
    blocks = doc.pages[0].blocks
    paths = [(type(b).__name__, b.heading_path) for b in blocks]
    assert paths == [
        ("Heading", ("Title",)),
        ("Paragraph", ("Title",)),
        ("Heading", ("Title", "Section A")),
        ("Heading", ("Title", "Section A", "Sub A1")),
        ("Paragraph", ("Title", "Section A", "Sub A1")),
        ("Heading", ("Title", "Section B")),
        ("Paragraph", ("Title", "Section B")),
        ("Heading", ("Title", "Section B", "Deep")),
        ("Paragraph", ("Title", "Section B", "Deep")),
        ("Heading", ("Next Title",)),
        ("Paragraph", ("Next Title",)),
    ]
    assert [b.level for b in blocks if isinstance(b, Heading)] == [1, 2, 3, 2, 4, 1]


def test_heading_stack_carries_across_page_breaks():
    doc = parse_di_markdown("# Chapter\n\n## Part\n\n<!-- PageBreak -->\n\ncontinued\n")
    assert doc.pages[1].blocks[0].heading_path == ("Chapter", "Part")


def test_content_before_any_heading_has_empty_path():
    doc = parse_di_markdown("preface\n\n# T\n")
    assert doc.pages[0].blocks[0].heading_path == ()


def test_heading_syntax_edge_cases():
    doc = parse_di_markdown(
        "#1 in market\n\n####### seven\n\n## Closed ##\n\n## #hash & more\n\n#\n"
    )
    assert _texts(doc.pages[0]) == [
        ("Paragraph", "#1 in market"),  # 无空格 → 不是标题
        ("Paragraph", "####### seven"),  # 超过 6 级 → 不是标题
        ("Heading", "Closed"),  # 可选的闭合 # 序列被去掉
        ("Heading", "#hash & more"),
        ("Heading", ""),  # 空标题合法
    ]


def test_heading_line_ends_a_paragraph_without_blank_line():
    doc = parse_di_markdown("para line\n# Head\nafter\n")
    assert _texts(doc.pages[0]) == [
        ("Paragraph", "para line"),
        ("Heading", "Head"),
        ("Paragraph", "after"),
    ]


# ---- figure -------------------------------------------------------------------------------


def test_figure_is_one_block_with_caption_and_text():
    doc = parse_di_markdown(
        "# Chart\n\n<figure>\n<figcaption>Figure 2 This is a figure</figcaption>\n\n"
        "Values\n300\n\n&lt;1% Jan Feb\n\n</figure>\n\nThis is footnote.\n"
    )
    blocks = doc.pages[0].blocks
    assert [type(b) for b in blocks] == [Heading, Figure, Paragraph]
    fig = blocks[1]
    assert fig.caption == "Figure 2 This is a figure"
    assert fig.text == "Values\n300\n<1% Jan Feb"
    assert fig.heading_path == ("Chart",)
    assert blocks[2].text == "This is footnote."


def test_figure_without_caption_and_unclosed_figure():
    doc = parse_di_markdown("<figure>\n\nA\nB\n\n</figure>\n<!-- PageBreak -->\n<figure>\nC\n\nD")
    assert doc.pages[0].blocks == (Figure(text="A\nB", caption=None, heading_path=()),)
    assert doc.pages[1].blocks == (Figure(text="C\nD", caption=None, heading_path=()),)


# ---- 表格 ---------------------------------------------------------------------------------


def test_table_block_with_trailing_footnote_and_heading_path():
    doc = parse_di_markdown(
        "## Results\n\n<table>\n<caption>Table 1. Demo</caption>\n"
        '<tr><th rowspan="2">%</th><th colspan="2">H1</th></tr>\n'
        "<tr><th>A</th><th>B</th></tr>\n<tr><td>x &amp; y</td><td>1</td><td>2</td></tr>\n"
        "</table>\nThis is the footnote of the table.\n"
    )
    blocks = doc.pages[0].blocks
    assert [type(b) for b in blocks] == [Heading, Table, Paragraph]
    table = blocks[1]
    assert table.heading_path == ("Results",)
    assert table.grid.caption == "Table 1. Demo"
    assert table.grid.rows == (("%", "H1", "H1"), ("%", "A", "B"), ("x & y", "1", "2"))
    assert blocks[2].text == "This is the footnote of the table."


def test_consecutive_tables_are_separate_blocks():
    doc = parse_di_markdown(
        "<table><tr><td>1</td></tr></table>\n\n<table><tr><td>2</td></tr></table>"
    )
    tables = [b for b in doc.pages[0].blocks if isinstance(b, Table)]
    assert [t.grid.rows for t in tables] == [(("1",),), (("2",),)]


def test_unclosed_table_stops_at_the_page_break():
    doc = parse_di_markdown(
        "<table>\n<tr><td>a</td><td>b\n\n<tr><td>c</td>\n<!-- PageBreak -->\nnext page\n"
    )
    assert len(doc.pages) == 2
    (table,) = doc.pages[0].blocks
    assert isinstance(table, Table)
    assert table.grid.rows == (("a", "b"), ("c", ""))
    assert _texts(doc.pages[1]) == [("Paragraph", "next page")]


# ---- 畸形输入 -----------------------------------------------------------------------------


def test_malformed_markup_never_raises_and_keeps_text():
    doc = parse_di_markdown(
        "<!-- unterminated comment\n\nstray </table> and </figure>\n\n"
        '<!-- PageNumber="5"\n\n<td>orphan cell</td>\n'
    )
    texts = [t for _, t in _texts(doc.pages[0])]
    assert texts == [
        "<!-- unterminated comment",
        "stray </table> and </figure>",
        '<!-- PageNumber="5"',
        "<td>orphan cell</td>",
    ]
    assert doc.pages[0].number == 1


def test_crlf_line_endings_are_normalized():
    doc = parse_di_markdown("# T\r\n\r\npara\r\n<!-- PageBreak -->\r\nnext\r\n")
    assert _texts(doc.pages[0]) == [("Heading", "T"), ("Paragraph", "para")]
    assert _texts(doc.pages[1]) == [("Paragraph", "next")]


# ---- `<!-- page: N -->` 页标记模式（SuperIndex azure_di 抽取器的输出）--------------------------


def _mark(n: int | str) -> str:
    # 与 SuperIndex azure_di.to_markdown 插入的形状一致：前后各一个换行
    return f"\n<!-- page: {n} -->\n"


def test_has_page_markers_needs_a_whole_comment_body():
    assert has_page_markers("a <!-- page: 3 --> b")
    assert has_page_markers("<!--page:12-->")
    assert has_page_markers("<!--  PAGE:  4  -->")
    assert not has_page_markers("a\n<!-- PageBreak -->\nb")
    assert not has_page_markers('<!-- PageNumber="3" -->')
    assert not has_page_markers("<!-- page: x -->")
    assert not has_page_markers("<!-- see page: 3 -->")
    assert not has_page_markers("")


def test_page_marker_numbers_are_distinct_and_zero_counts_as_one():
    text = f"{_mark(0)}a{_mark(5)}b{_mark(5)}c{_mark(1)}<!-- PageBreak -->"
    assert page_marker_numbers(text) == frozenset({1, 5})
    assert page_marker_numbers("a\n<!-- PageBreak -->\nb") == frozenset()


def test_page_numbers_above_the_cap_are_not_markers():
    assert MAX_MARKER_PAGE == 10_000
    assert has_page_markers(_mark(MAX_MARKER_PAGE))
    for huge in (MAX_MARKER_PAGE + 1, 99_999_999, "9" * 5000, "0" * 20 + "10001"):
        text = f"one{_mark(huge)}two\n<!-- PageBreak -->\nthree"
        assert not has_page_markers(text)
        assert page_marker_numbers(text) == frozenset()
        start = time.perf_counter()
        doc = parse_di_markdown(text)
        assert time.perf_counter() - start < 1.0
        # 回到 PageBreak 模式，超限标记按普通注释剥掉
        assert [_texts(p) for p in doc.pages] == [
            [("Paragraph", "one\ntwo")],
            [("Paragraph", "three")],
        ]


def test_page_numbers_above_the_cap_do_not_disturb_valid_markers():
    text = f"{_mark(2)}two{_mark(99_999_999)}still two{_mark(3)}three"
    assert page_marker_numbers(text) == frozenset({2, 3})
    start = time.perf_counter()
    doc = parse_di_markdown(text)
    assert time.perf_counter() - start < 1.0
    assert [p.index for p in doc.pages] == [1, 2, 3]
    assert [t for _, t in _texts(doc.pages[1])] == ["two\nstill two"]
    assert _texts(doc.pages[2]) == [("Paragraph", "three")]


def test_page_markers_only_split_pages_and_index_is_the_marker():
    doc = parse_di_markdown(f"{_mark(1)}# One\n\nbody one\n{_mark(2)}body two\n")
    assert [p.index for p in doc.pages] == [1, 2]
    assert [_texts(p) for p in doc.pages] == [
        [("Heading", "One"), ("Paragraph", "body one")],
        [("Paragraph", "body two")],
    ]
    assert doc.pages[1].blocks[0].heading_path == ("One",)  # 标题栈按页序延续


def test_page_marker_wins_and_page_break_is_dropped():
    doc = parse_di_markdown(
        f"{_mark(1)}one\n\n<!-- PageBreak -->\n{_mark(2)}two\n<!-- PageBreak -->\n{_mark(3)}three"
    )
    assert [p.index for p in doc.pages] == [1, 2, 3]
    assert [_texts(p) for p in doc.pages] == [
        [("Paragraph", "one")],
        [("Paragraph", "two")],
        [("Paragraph", "three")],
    ]


def test_missing_pages_are_filled_with_empty_pages():
    doc = parse_di_markdown(f"{_mark(2)}page two\n{_mark(4)}page four\n")
    assert [p.index for p in doc.pages] == [1, 2, 3, 4]
    assert [p.number for p in doc.pages] == [1, 2, 3, 4]
    assert doc.pages[0].blocks == () and doc.pages[2].blocks == ()
    assert _texts(doc.pages[1]) == [("Paragraph", "page two")]
    assert _texts(doc.pages[3]) == [("Paragraph", "page four")]


def test_partial_analysis_keeps_true_pdf_page_numbers():
    text = "".join(f"{_mark(n)}# P{n}\n\nbody {n}\n" for n in range(5, 21))
    doc = parse_di_markdown(text)
    assert len(doc.pages) == 20
    assert doc.pages[4].index == 5
    assert _texts(doc.pages[4]) == [("Heading", "P5"), ("Paragraph", "body 5")]
    assert all(p.blocks == () for p in doc.pages[:4])


def test_page_number_label_sets_number_while_marker_sets_index():
    doc = parse_di_markdown(
        f'{_mark(7)}body\n<!-- PageNumber="iii" -->{_mark(8)}more\n<!-- PageNumber="42" -->'
    )
    assert [(p.index, p.number, p.page_number_label) for p in doc.pages[6:]] == [
        (7, 7, "iii"),
        (8, 42, "42"),
    ]


def test_leading_content_belongs_to_the_first_marked_page():
    doc = parse_di_markdown(f"Intro before any marker.\n{_mark(2)}Page two.\n")
    assert len(doc.pages) == 2
    assert doc.pages[0].blocks == ()
    assert _texts(doc.pages[1]) == [
        ("Paragraph", "Intro before any marker."),
        ("Paragraph", "Page two."),
    ]


def test_inline_marker_splits_the_line():
    doc = parse_di_markdown("alpha <!-- page: 1 --> beta <!--page:2--> gamma")
    assert [p.index for p in doc.pages] == [1, 2]
    assert [t for _, t in _texts(doc.pages[0])] == ["alpha", "beta"]
    assert _texts(doc.pages[1]) == [("Paragraph", "gamma")]


def test_abnormal_page_numbers_zero_repeats_and_reverse_order():
    doc = parse_di_markdown(
        f"{_mark(0)}zero\n{_mark(3)}three a\n{_mark(2)}two\n{_mark(3)}three b\n"
    )
    assert [p.index for p in doc.pages] == [1, 2, 3]
    assert _texts(doc.pages[0]) == [("Paragraph", "zero")]  # page: 0 按第 1 页
    assert _texts(doc.pages[1]) == [("Paragraph", "two")]
    # 同一页号的内容按文档顺序拼接
    assert _texts(doc.pages[2]) == [("Paragraph", "three a"), ("Paragraph", "three b")]


_HEAD = (
    '<tr><th rowspan="2">Metric</th><th colspan="2">FY</th></tr>\n'
    "<tr><th>2024</th><th>2025</th></tr>\n"
)
_ROWS = [
    "<tr><td>Revenue</td><td>100</td><td>110</td></tr>\n",
    "<tr><td>Profit</td><td>30</td><td>33</td></tr>\n",
    "<tr><td>Margin</td><td>7.5</td><td>8.25</td></tr>\n",
]


def _data_rows(grid):
    return grid.rows[grid.header_row_count :]


def _only_table(page):
    (table,) = [b for b in page.blocks if isinstance(b, Table)]
    return table.grid


def _numbers(*grids):
    """数值格（非表头、可解析为数字的锚点格文本），多重集比较用。"""
    out = []
    for grid in grids:
        for cell in grid.cells:
            try:
                float(cell.text)
            except ValueError:
                continue
            if not cell.is_header:
                out.append(cell.text)
    return sorted(out)


def _aligned(grid, original):
    """列对齐：每一行的列数与原表一致，且每个非空位置的文字等于原表同列某行的文字。"""
    assert grid.n_cols == original.n_cols
    for row in grid.rows:
        assert len(row) == original.n_cols
        for c, text in enumerate(row):
            assert text == "" or text in {r[c] for r in original.rows}


def _split_pages(split, *indexes):
    doc = parse_di_markdown(split)
    return doc, [_only_table(doc.pages[i - 1]) for i in indexes]


def test_marker_inside_a_table_reopens_it_with_the_header_rows():
    whole = f"## Results\n\n<table>\n{_HEAD}{''.join(_ROWS)}</table>\nFootnote.\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    split = (
        f"{_mark(1)}## Results\n\n<table>\n{_HEAD}{_ROWS[0]}"
        f"{_mark(2)}{''.join(_ROWS[1:])}</table>\nFootnote.\n"
    )
    doc, (first, second) = _split_pages(split, 1, 2)

    # 补上的表头与原表头文字一致、行数一致，且不带任何数值格
    head_rows = original.header_row_count
    assert head_rows == 2
    assert first.header_row_count == second.header_row_count == head_rows
    assert first.rows[:head_rows] == second.rows[:head_rows] == original.rows[:head_rows]
    assert [c for c in second.cells if c.row < head_rows and not c.is_header] == []
    # 两页数值格合起来与原表完全一致：不重复、不丢失；每一行数字落在它真实所在的页
    assert _numbers(first, second) == _numbers(original)
    assert _data_rows(first) == (("Revenue", "100", "110"),)
    assert _data_rows(second) == (("Profit", "30", "33"), ("Margin", "7.5", "8.25"))
    assert [type(b) for b in doc.pages[1].blocks] == [Table, Paragraph]
    assert doc.pages[1].blocks[0].heading_path == ("Results",)
    assert _texts(doc.pages[1])[-1] == ("Paragraph", "Footnote.")


def test_marker_in_the_middle_of_a_td_keeps_the_value_on_its_page():
    whole = f"<table>\n{_HEAD}{''.join(_ROWS[:2])}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    split = (
        f"{_mark(1)}<table>\n{_HEAD}<tr><td>Revenue</td><td>"
        f"<!-- page: 2 -->100</td><td>110</td></tr>\n{_ROWS[1]}</table>\n"
    )
    _, (first, second) = _split_pages(split, 1, 2)
    assert _data_rows(first) == (("Revenue", "", ""),)
    # 被切开的行：行标签（第 0 列 td 文本）补到第 2 页，数值不复制
    assert _data_rows(second) == (("Revenue", "100", "110"), ("Profit", "30", "33"))
    assert _numbers(first, second) == _numbers(original)
    _aligned(first, original)
    _aligned(second, original)


def test_marker_in_the_middle_of_a_th_or_tr_keeps_values():
    split = (
        f"{_mark(1)}<table>\n<tr><th>Metric</th><th>2024</th><th>2025</th></tr>\n{_ROWS[0]}"
        "<tr><th>Profit<!-- page: 2 --></th><td>30</td><td>33</td></tr>\n"
        "<tr><!-- page: 3 --><td>Margin</td><td>7.5</td><td>8.25</td></tr>\n</table>\n"
    )
    whole = split.replace("<!-- page: 2 -->", "").replace("<!-- page: 3 -->", "")
    original = _only_table(parse_di_markdown(whole.replace(_mark(1), "")).pages[0])
    _, grids = _split_pages(split, 1, 2, 3)
    for grid in grids:
        assert grid.rows[:1] == original.rows[:1]  # 表头各页完整
        assert grid.header_row_count == 1
        _aligned(grid, original)
    assert _numbers(*grids) == _numbers(original)
    assert [_data_rows(g) for g in grids] == [
        (("Revenue", "100", "110"), ("Profit", "", "")),  # th 的文字在标记之前
        (("Profit", "30", "33"),),  # 同一行其余的格在第 2 页，th 行标签补上
        (("Margin", "7.5", "8.25"),),  # 标记在 <tr> 与 <td> 之间：整行在第 3 页
    ]


_HEAD4 = "<tr><th>Metric</th><th>2023</th><th>2024</th><th>2025</th></tr>\n"


def _cut_row(row: str):
    whole = f"<table>\n{_HEAD4}{row.format(m2='', m3='')}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    split = f"{_mark(1)}<table>\n{_HEAD4}{row.format(m2=_mark(2), m3=_mark(3))}</table>\n"
    doc = parse_di_markdown(split)
    grids = [_only_table(p) for p in doc.pages]
    assert _numbers(*grids) == _numbers(original)  # 数值格多重集不变
    for grid in grids:
        _aligned(grid, original)
    return original, grids


def test_cut_row_th_label_is_copied_to_the_next_page():
    original, (first, second) = _cut_row(
        "<tr><th>Revenue</th><td>90</td><td>100</td>{m2}<td>110</td></tr>\n"
    )
    assert _data_rows(first) == (("Revenue", "90", "100", ""),)
    assert _data_rows(second) == (("Revenue", "", "", "110"),)
    assert _data_rows(second)[0][0] == original.rows[1][0]
    (label,) = [c for c in second.cells if c.text == "Revenue"]
    assert (label.col, label.is_header) == (0, True)


def test_cut_row_td_text_label_in_column_zero_is_copied():
    _, (_, second) = _cut_row("<tr><td>Revenue</td><td>90</td>{m2}<td>100</td><td>110</td></tr>\n")
    assert _data_rows(second) == (("Revenue", "", "100", "110"),)


def test_cut_row_numeric_column_zero_is_not_copied():
    _, (first, second) = _cut_row("<tr><td>2022</td><td>90</td>{m2}<td>100</td><td>110</td></tr>\n")
    assert _data_rows(first) == (("2022", "90", "", ""),)
    assert _data_rows(second) == (("", "", "100", "110"),)
    _, (_, second) = _cut_row("<tr><td>1,234.5%</td><td>90</td>{m2}<td>100</td><td>110</td></tr>\n")
    assert _data_rows(second) == (("", "", "100", "110"),)


def test_cut_row_across_three_pages_gets_its_label_on_each():
    _, grids = _cut_row("<tr><td>Revenue</td><td>90</td>{m2}<td>100</td>{m3}<td>110</td></tr>\n")
    assert [_data_rows(g) for g in grids] == [
        (("Revenue", "90", "", ""),),
        (("Revenue", "", "100", ""),),
        (("Revenue", "", "", "110"),),
    ]


def test_cut_row_rowspan_label_is_emitted_once_per_page():
    _, (first, second) = _cut_row(
        '<tr><td rowspan="2">Asia</td><td>90</td>{m2}<td>100</td><td>110</td></tr>\n'
        "<tr><td>91</td><td>101</td><td>111</td></tr>\n"
    )
    assert _data_rows(first) == (("Asia", "90", "", ""),)
    assert _data_rows(second) == (("Asia", "", "100", "110"), ("Asia", "91", "101", "111"))
    (label,) = [c for c in second.cells if c.text == "Asia"]
    assert (label.col, label.row_span) == (0, 2)


def test_header_rowspan_into_data_rows_is_clipped_to_the_header_block():
    table = (
        '<tr><th rowspan="3">Metric</th><th>FY2024</th></tr>\n'
        "<tr><td>100</td></tr>\n{m}<tr><td>110</td></tr>\n"
    )
    whole = f"<table>\n{table.format(m='')}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    assert original.header_row_count == 1
    _, (first, second) = _split_pages(
        f"{_mark(1)}<table>\n{table.format(m=_mark(2))}</table>\n", 1, 2
    )
    assert first.rows == (("Metric", "FY2024"), ("Metric", "100"))
    assert second.rows == (("Metric", "FY2024"), ("Metric", "110"))
    assert first.header_row_count == second.header_row_count == 1
    assert _numbers(first, second) == _numbers(original)
    _aligned(first, original)
    _aligned(second, original)


def test_data_rowspan_across_pages_repeats_its_anchor_once_per_page():
    table = (
        "<tr><th>Region</th><th>Metric</th><th>Value</th></tr>\n"
        '<tr><td rowspan="3">Asia</td><td>Revenue</td><td>100</td></tr>\n'
        "<tr><td>Profit</td><td>30</td></tr>\n{m}<tr><td>Margin</td><td>7.5</td></tr>\n"
        "<tr><td>Europe</td><td>Revenue</td><td>80</td></tr>\n"
    )
    whole = f"<table>\n{table.format(m='')}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    _, (first, second) = _split_pages(
        f"{_mark(1)}<table>\n{table.format(m=_mark(2))}</table>\n", 1, 2
    )
    assert _data_rows(first) == (("Asia", "Revenue", "100"), ("Asia", "Profit", "30"))
    assert _data_rows(second) == (("Asia", "Margin", "7.5"), ("Europe", "Revenue", "80"))
    assert _numbers(first, second) == _numbers(original)
    # 锚点格（行标签）在两页各出现一次
    assert [c.text for g in (first, second) for c in g.cells if c.text == "Asia"] == [
        "Asia",
        "Asia",
    ]
    _aligned(first, original)
    _aligned(second, original)


def test_header_and_data_rowspans_across_pages_together():
    table = (
        '<tr><th rowspan="4">Metric</th><th colspan="2">FY</th></tr>\n'
        '<tr><td rowspan="3">Asia</td><td>100</td></tr>\n'
        "<tr><td>110</td></tr>\n{m}<tr><td>120</td></tr>\n"
    )
    whole = f"<table>\n{table.format(m='')}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    assert original.header_row_count == 1
    _, (first, second) = _split_pages(
        f"{_mark(1)}<table>\n{table.format(m=_mark(2))}</table>\n", 1, 2
    )
    assert first.rows == original.rows[:3]
    assert second.rows == (original.rows[0], original.rows[3])
    assert first.header_row_count == second.header_row_count == 1
    assert _numbers(first, second) == _numbers(original)
    _aligned(first, original)
    _aligned(second, original)


def test_comments_inside_a_split_table_stay_on_their_page():
    doc = parse_di_markdown(
        f"{_mark(1)}<table>\n<caption>T1</caption>\n{_HEAD}{_ROWS[0]}"
        f'<!-- PageFooter="F1" -->{_mark(2)}<!-- PageHeader="H2" -->\n{_ROWS[1]}</table>\n'
    )
    assert (doc.pages[0].footers, doc.pages[1].headers) == (("F1",), ("H2",))
    assert _only_table(doc.pages[0]).caption == "T1"
    assert _only_table(doc.pages[1]).caption is None
    assert _data_rows(_only_table(doc.pages[1])) == (("Profit", "30", "33"),)


def test_stray_closing_tags_and_inline_tables_are_left_alone():
    doc = parse_di_markdown(
        f"{_mark(1)}stray </table> text\nsee <table><tr><td>1</td>{_mark(2)}<td>2</td></tr></table>\n"
    )
    assert [p.index for p in doc.pages] == [1, 2]
    assert not any(isinstance(b, Table) for p in doc.pages for b in p.blocks)


def test_never_closed_table_is_not_carried_to_later_pages():
    doc = parse_di_markdown(
        f"{_mark(1)}<table>\n{_HEAD}{_ROWS[0]}{_mark(2)}{_ROWS[1]}{_mark(3)}Page three text.\n"
    )
    assert _data_rows(_only_table(doc.pages[0])) == (("Revenue", "100", "110"),)
    assert not any(isinstance(b, Table) for p in doc.pages[1:] for b in p.blocks)
    assert _texts(doc.pages[2]) == [("Paragraph", "Page three text.")]


def test_table_spanning_three_pages_keeps_every_row_once():
    whole = f"<table>\n{_HEAD}{''.join(_ROWS)}</table>\n"
    original = _only_table(parse_di_markdown(whole).pages[0])
    split = (
        f"{_mark(4)}<table>\n{_HEAD}{_ROWS[0]}{_mark(5)}{_ROWS[1]}{_mark(6)}{_ROWS[2]}</table>\n"
    )
    doc = parse_di_markdown(split)
    grids = [_only_table(doc.pages[i]) for i in (3, 4, 5)]
    assert all(g.rows[:2] == original.rows[:2] for g in grids)
    assert tuple(r for g in grids for r in _data_rows(g)) == _data_rows(original)
    assert [_data_rows(g)[0][0] for g in grids] == ["Revenue", "Profit", "Margin"]


def test_marker_inside_a_headerless_table_reopens_only_the_table():
    doc = parse_di_markdown(f"{_mark(1)}<table>\n{_ROWS[0]}{_mark(2)}{_ROWS[1]}</table>\n")
    assert _only_table(doc.pages[0]).rows == (("Revenue", "100", "110"),)
    assert _only_table(doc.pages[1]).rows == (("Profit", "30", "33"),)


def test_marker_after_a_closed_table_adds_nothing():
    doc = parse_di_markdown(f"{_mark(1)}<table>\n{_HEAD}{_ROWS[0]}</table>\n{_mark(2)}after\n")
    assert _texts(doc.pages[1]) == [("Paragraph", "after")]


# ---- 数值判断（行标签过滤）与纯 stdlib 契约 --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "2022",
        "100",
        "1,234",
        "1,234.56",
        "1 234",
        "1\u00a0234",
        "0.5",
        ".5",
        "+12",
        "-12",
        "\u221212",
        "\u201312",
        "(130)",
        "(1,234.5)",
        "(12%)",
        "12%",
        "12 %",
        "$1,234",
        "US$ 5",
        "HK$1,234",
        "RMB 12.3",
        "¥100",
        "€ 3.5",
        "£12",
        "12bn",
        "12 BN",
        "3m",
        "4.5mn",
        "7k",
        "1.2x",
        "30pp",
        "25 bps",
        "12*",
        "12**",
        "12\u00b9",
        "\uff11\uff12",
        "\uff04\uff11\uff12",
        "-$12bn",
        "$(12)",
        "12 USD",
    ],
)
def test_is_value_like_accepts_numeric_forms(text):
    assert _is_value_like(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "-",
        "—",
        "n/a",
        "*",
        "()",
        "$",
        "Revenue",
        "2023 年上半年",
        "Q1",
        "1H24",
        "FY2024",
        "Asia (ex-China)",
        "Note 3",
        "3 of 10",
        "Total",
        "12 months",
        "2024-06-30",
        "12-15",
    ],
)
def test_is_value_like_rejects_labels(text):
    assert not _is_value_like(text)


def test_di_markdown_imports_only_stdlib_and_itself():
    root = ROOT_DIR / "src" / "ragspine" / "extraction" / "di_markdown"
    allowed = ("ragspine.extraction.di_markdown", "ragspine")
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                ok = (
                    name.split(".")[0] in sys.stdlib_module_names
                    or name in allowed
                    or (name.startswith("ragspine.extraction.di_markdown."))
                )
                assert ok, f"{path.name} imports {name}"
