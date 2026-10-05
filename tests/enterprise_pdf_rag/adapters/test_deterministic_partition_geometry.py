"""确定性版面切分的纯几何工具: 聚行 / 跨页重复 / 页边带 / 栏判定 / 块分组."""

from enterprise_pdf_rag.adapters.deterministic_partition_geometry import (
    BlockSpec,
    ColumnLayout,
    TextLine,
    body_blocks,
    body_font_size,
    bullet_marker,
    column_layout,
    gutters,
    is_page_number,
    running_lines,
    split_margin_lines,
    text_lines,
)
from ragspine.extraction.evidence.document.models import TextSpan

PAGE_HEIGHT = 800.0


def _span(
    span_id: str,
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    size: float = 10.0,
    font: str = "Helvetica",
) -> TextSpan:
    return TextSpan(span_id, text, (x0, y0, x1, y1), (x0, y1), font, size)


def _line_span(
    span_id: str,
    text: str,
    top: float,
    *,
    x0: float = 50.0,
    size: float = 10.0,
    font: str = "Helvetica",
) -> TextSpan:
    return _span(
        span_id, text, x0, top, x0 + 8.0 * max(len(text), 1), top + size, size=size, font=font
    )


# ---- text_lines ---------------------------------------------------------------------------


def test_text_lines_merges_vertically_overlapping_spans_and_sorts_by_x() -> None:
    right = _span("s1", "1,234", 200.0, 100.0, 240.0, 110.0)
    left = _span("s0", "Revenue", 50.0, 101.0, 110.0, 111.0)
    below = _span("s2", "Margin", 50.0, 120.0, 100.0, 130.0)
    lines = text_lines((right, below, left))
    assert [line.span_ids for line in lines] == [("s0", "s1"), ("s2",)]
    assert lines[0].text == "Revenue 1,234"
    assert lines[0].top == 100.0
    assert lines[0].size == 10.0


def test_text_lines_keeps_distinct_lines_with_small_overlap_apart() -> None:
    # 两行只有不到较矮者一半的竖直重叠: 不并行.
    first = _span("a", "alpha", 50.0, 100.0, 90.0, 110.0)
    second = _span("b", "beta", 50.0, 106.0, 90.0, 116.0)
    assert [line.span_ids for line in text_lines((first, second))] == [("a",), ("b",)]


# ---- running_lines ------------------------------------------------------------------------


def test_running_lines_need_thirty_percent_and_two_pages() -> None:
    header = [_line_span(f"h{i}", "ACME Interim Report", 20.0) for i in range(10)]
    pages = [(PAGE_HEIGHT, (header[i], _line_span(f"b{i}", f"Body {i}", 400.0))) for i in range(10)]
    keys = running_lines(pages)
    assert ("ACME Interim Report", 4) in keys
    assert all(key[0] != "Body 0" for key in keys)


def test_a_line_on_one_page_of_many_is_not_running() -> None:
    pages = [
        (PAGE_HEIGHT, (_line_span(f"p{i}", "Once only" if i == 0 else f"Body {i}", 20.0),))
        for i in range(10)
    ]
    assert running_lines(pages) == frozenset()


def test_two_identical_pages_make_their_lines_running() -> None:
    pages = [(PAGE_HEIGHT, (_line_span(f"p{i}", "Footer note", 780.0),)) for i in range(2)]
    assert ("Footer note", 156) in running_lines(pages)


# ---- page numbers and margin bands --------------------------------------------------------


def test_page_number_shapes() -> None:
    for text in ("3", "12", "Page 3", "page 3 of 12", "3 / 12", "- 7 -", "第 3 页"):
        assert is_page_number(text), text
    for text in ("Revenue 3", "2026", "Chapter 3 overview", ""):
        assert not is_page_number(text), text


def test_margin_split_keeps_running_header_and_page_number_apart_from_body() -> None:
    header = _line_span("h", "ACME Interim Report", 12.0)
    body = _line_span("b", "Narrative body text", 300.0)
    number = _line_span("n", "7", 784.0)
    lines = text_lines((header, body, number))
    split = split_margin_lines(
        lines,
        page_height=PAGE_HEIGHT,
        running=frozenset({("ACME Interim Report", round(12.0 / 5.0))}),
    )
    assert [line.text for line in split.header] == ["ACME Interim Report"]
    assert [line.text for line in split.body] == ["Narrative body text"]
    assert [line.text for line in split.footer] == ["7"]


def test_a_non_running_line_in_the_top_band_stays_body() -> None:
    title = _line_span("t", "Results overview", 12.0, size=18.0)
    split = split_margin_lines(text_lines((title,)), page_height=PAGE_HEIGHT, running=frozenset())
    assert not split.header and [line.text for line in split.body] == ["Results overview"]


# ---- gutters and column layout ------------------------------------------------------------


def _rows_page() -> tuple[TextLine, ...]:
    spans = []
    for row, (label, value) in enumerate(
        (("Revenue", "1,234"), ("Margin", "12%"), ("Expenses", "456"), ("Profit", "778"))
    ):
        top = 100.0 + 20.0 * row
        spans.append(_span(f"l{row}", label, 50.0, top, 50.0 + 8.0 * len(label), top + 10.0))
        spans.append(_span(f"v{row}", value, 300.0, top, 300.0 + 8.0 * len(value), top + 10.0))
    return text_lines(tuple(spans))


def _narrative_columns_page(*, aligned: bool) -> tuple[TextLine, ...]:
    spans = []
    text = "Narrative sentence that fills the whole column width here"
    for row in range(8):
        top = 100.0 + 14.0 * row
        spans.append(_span(f"l{row}", text, 40.0, top, 280.0, top + 10.0))
        right_top = top if aligned else top + 7.0
        spans.append(_span(f"r{row}", text, 320.0, right_top, 560.0, right_top + 10.0))
    return text_lines(tuple(spans))


def test_single_column_page_has_no_gutter_and_reads_in_rows() -> None:
    lines = text_lines(
        tuple(_line_span(f"s{i}", f"Paragraph line {i}", 100.0 + 14.0 * i) for i in range(5))
    )
    assert gutters(lines) == ()
    assert column_layout(lines) == ColumnLayout("rows", None)


def test_label_value_rows_with_a_full_height_gap_stay_rows() -> None:
    lines = _rows_page()
    assert len(gutters(lines)) == 1
    assert column_layout(lines).mode == "rows"


def test_two_narrative_columns_with_unaligned_baselines_are_columns() -> None:
    layout = column_layout(_narrative_columns_page(aligned=False))
    assert layout.mode == "columns"
    assert layout.boundary is not None and 280.0 < layout.boundary < 320.0


def test_two_aligned_narrative_columns_are_ambiguous_not_interleaved() -> None:
    assert column_layout(_narrative_columns_page(aligned=True)).mode == "ambiguous"


# ---- bullets, headings and blocks ---------------------------------------------------------


def test_bullet_markers() -> None:
    assert bullet_marker("• First point") == "symbol"
    assert bullet_marker("- dash item") == "symbol"
    assert bullet_marker("1. Numbered") == "number"
    assert bullet_marker("12) Also numbered") == "number"
    assert bullet_marker("Plain sentence") is None
    assert bullet_marker("3.5% growth") is None


def test_body_font_size_is_the_char_weighted_mode() -> None:
    lines = text_lines(
        (
            _line_span("a", "Big heading", 100.0, size=18.0),
            _line_span("b", "A much longer body sentence for weighting", 130.0, size=10.0),
            _line_span("c", "Another long body sentence to weigh more", 145.0, size=10.0),
        )
    )
    assert body_font_size(lines) == 10.0


def test_heading_starts_a_new_object_and_owns_the_following_paragraph() -> None:
    lines = text_lines(
        (
            _line_span("p0", "Intro paragraph line", 80.0),
            _line_span("h1", "Section heading", 120.0, size=16.0),
            _line_span("p1", "First body line of the section", 140.0),
            _line_span("p2", "Second body line of the section", 152.0),
            _line_span("h2", "Next heading", 190.0, size=16.0),
            _line_span("p3", "Body of the next section", 210.0),
        )
    )
    blocks = body_blocks(lines, body_size=body_font_size(lines))
    assert [block.kind for block in blocks] == ["text", "text", "text"]
    assert [line.text for line in blocks[1].lines] == [
        "Section heading",
        "First body line of the section",
        "Second body line of the section",
    ]
    assert [line.text for line in blocks[2].lines] == ["Next heading", "Body of the next section"]


def test_bullet_run_becomes_a_list_with_items_and_a_single_bullet_stays_text() -> None:
    lines = text_lines(
        (
            _line_span("h", "Highlights", 100.0, size=16.0),
            _line_span("b1", "• Revenue grew strongly", 130.0),
            _line_span("b1c", "across every market", 142.0, x0=62.0),
            _line_span("b2", "• Margin improved", 160.0),
            _line_span("t", "Outlook remains stable", 190.0),
        )
    )
    blocks = body_blocks(lines, body_size=body_font_size(lines))
    assert [block.kind for block in blocks] == ["text", "list", "text"]
    list_block = blocks[1]
    assert isinstance(list_block, BlockSpec)
    assert [tuple(line.text for line in item) for item in list_block.items] == [
        ("• Revenue grew strongly", "across every market"),
        ("• Margin improved",),
    ]
    assert list_block.ordered is False
    single = body_blocks(text_lines((_line_span("s", "• Lone bullet", 100.0),)), body_size=10.0)
    assert [block.kind for block in single] == ["text"]


def test_numbered_items_make_an_ordered_list() -> None:
    lines = text_lines(
        (
            _line_span("n1", "1. First step", 100.0),
            _line_span("n2", "2. Second step", 120.0),
        )
    )
    blocks = body_blocks(lines, body_size=10.0)
    assert [block.kind for block in blocks] == ["list"]
    assert blocks[0].ordered is True
