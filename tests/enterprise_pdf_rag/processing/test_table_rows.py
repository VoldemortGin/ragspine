"""Verbatim row transcription of an unproved table region (ADR 00NN): pure geometry only."""

from dataclasses import replace

import pytest

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.objects.tables.table_rows import (
    ROW_SEPARATOR,
    TABLE_ROWS_PRODUCER,
    TABLE_ROWS_SCOPE,
    check_table_rows,
    group_rows,
    rows_text,
    table_rows,
)

_ANCHOR = SourceAnchor("a" * 64, "a" * 64, 0, (10.0, 50.0, 390.0, 250.0))


def _span(span_id: str, text: str, x0: float, y0: float, x1: float, y1: float) -> TextSpan:
    return TextSpan(span_id, text, (x0, y0, x1, y1), size=y1 - y0)


def _texts(rows: tuple[tuple[TextSpan, ...], ...]) -> list[list[str]]:
    return [[span.text for span in row] for row in rows]


def test_a_slightly_offset_baseline_stays_on_its_row_and_cells_read_left_to_right() -> None:
    spans = (
        _span("s2", "1,234", 250.0, 100.8, 280.0, 110.8),
        _span("s1", "Revenue", 20.0, 100.0, 60.0, 110.0),
        _span("s3", "1,100", 320.0, 99.4, 350.0, 109.4),
    )
    assert _texts(group_rows(spans)) == [["Revenue", "1,234", "1,100"]]


def test_a_raised_footnote_marker_joins_its_line_and_keeps_its_place() -> None:
    spans = (
        _span("label", "Operating profit", 20.0, 100.0, 90.0, 110.0),
        _span("marker", "1", 91.0, 98.5, 94.0, 104.0),
        _span("value", "790,123", 250.0, 100.0, 285.0, 110.0),
        _span("next", "Total", 20.0, 116.0, 45.0, 126.0),
    )
    assert _texts(group_rows(spans)) == [["Operating profit", "1", "790,123"], ["Total"]]


def test_a_marker_above_the_next_line_does_not_pull_two_lines_together() -> None:
    spans = (
        _span("a", "Revenue", 20.0, 100.0, 60.0, 110.0),
        _span("m", "2", 70.0, 112.0, 73.0, 117.0),
        _span("b", "Cost of sales", 20.0, 114.0, 80.0, 124.0),
    )
    assert _texts(group_rows(spans)) == [["Revenue"], ["Cost of sales", "2"]]


def test_right_aligned_numbers_of_different_widths_keep_their_rows() -> None:
    spans = (
        _span("a1", "Revenue", 20.0, 100.0, 60.0, 110.0),
        _span("a2", "1,234,567", 235.0, 100.0, 285.0, 110.0),
        _span("b1", "Tax", 20.0, 114.0, 40.0, 124.0),
        _span("b2", "(56)", 265.0, 114.0, 285.0, 124.0),
    )
    assert _texts(group_rows(spans)) == [["Revenue", "1,234,567"], ["Tax", "(56)"]]


def test_a_label_wrapped_over_two_lines_stays_two_rows_and_nothing_is_merged() -> None:
    spans = (
        _span("l1", "Other operating income and", 20.0, 100.0, 140.0, 110.0),
        _span("l2", "expenses", 20.0, 112.0, 60.0, 122.0),
        _span("v", "12,345", 250.0, 112.0, 285.0, 122.0),
    )
    rows = group_rows(spans)
    assert _texts(rows) == [["Other operating income and"], ["expenses", "12,345"]]


def test_rows_are_ordered_top_down_whatever_the_page_order_and_ties_keep_page_order() -> None:
    spans = (
        _span("low", "Total", 20.0, 140.0, 45.0, 150.0),
        _span("high-b", "B", 30.0, 100.0, 40.0, 110.0),
        _span("high-a", "A", 30.0, 100.0, 40.0, 110.0),
    )
    assert _texts(group_rows(spans)) == [["B", "A"], ["Total"]]


def test_the_ir_keeps_every_character_and_separates_cells_with_a_tab() -> None:
    spans = (
        _span("h", "  Cost of sales ", 30.0, 100.0, 90.0, 110.0),
        _span("v", "(456,789)", 240.0, 100.0, 285.0, 110.0),
        _span("t", "Total", 20.0, 120.0, 45.0, 130.0),
    )
    ir = table_rows("table-1", _ANCHOR, spans)
    assert ir.producer == TABLE_ROWS_PRODUCER
    assert ROW_SEPARATOR == "\t"
    assert [row.row_id for row in ir.rows] == ["row-0", "row-1"]
    assert ir.rows[0].texts == ("  Cost of sales ", "(456,789)")
    assert ir.rows[0].source_span_ids == ("h", "v")
    assert ir.rows[0].bbox == (30.0, 100.0, 285.0, 110.0)
    assert ir.rows[0].text == "  Cost of sales \t(456,789)"
    assert rows_text(ir) == "  Cost of sales \t(456,789)\nTotal"
    assert ir.source_span_ids == ("h", "v", "t")
    assert TABLE_ROWS_SCOPE != "literal-source-transcription-v1"


def test_check_table_rows_rederives_the_rows_and_refuses_any_drift() -> None:
    spans = (
        _span("a", "Revenue", 20.0, 100.0, 60.0, 110.0),
        _span("b", "1,234", 250.0, 100.0, 280.0, 110.0),
        _span("c", "Total", 20.0, 120.0, 45.0, 130.0),
    )
    observed = {span.span_id: span for span in spans}
    ir = table_rows("table-1", _ANCHOR, spans)
    check_table_rows(ir, spans, anchor=_ANCHOR.bbox)

    altered = replace(ir, rows=(replace(ir.rows[0], texts=("Revenue", "1,235")), ir.rows[1]))
    with pytest.raises(ValueError, match="re-derive"):
        check_table_rows(altered, spans, anchor=_ANCHOR.bbox)
    merged = replace(
        ir,
        rows=(
            replace(
                ir.rows[0],
                source_span_ids=("a", "b", "c"),
                texts=("Revenue", "1,234", "Total"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="re-derive"):
        check_table_rows(merged, spans, anchor=_ANCHOR.bbox)
    with pytest.raises(ValueError, match="unknown source occurrence"):
        check_table_rows(ir, (observed["a"], observed["b"]), anchor=_ANCHOR.bbox)
    with pytest.raises(ValueError, match="outside"):
        check_table_rows(ir, spans, anchor=(10.0, 50.0, 100.0, 250.0))


def test_no_span_means_no_rows() -> None:
    with pytest.raises(ValueError, match="No source text"):
        table_rows("table-1", _ANCHOR, ())
