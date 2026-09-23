"""DI markdown → 类型化中间表示（页 → 块）的公开契约测试。

格式依据：Microsoft Learn「Document Intelligence supported Markdown elements」
（prebuilt-layout, outputContentFormat=markdown）——`<!-- PageBreak -->` 分页，
`<!-- PageHeader/PageFooter/PageNumber="..." -->` 为页元数据（不进正文），`#`–`######` 标题，
空行分段，HTML `<table>`，`<figure>`（可含 `<figcaption>`）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.models import Figure, Heading, Paragraph, Table
from ragspine.extraction.di_markdown.parse import parse_di_markdown


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
