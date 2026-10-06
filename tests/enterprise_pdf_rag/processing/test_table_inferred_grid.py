"""A model-inferred table grid filled from the text layer and kept PENDING (ADR 00NN): pure rules."""

from dataclasses import replace

import pytest

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_SCOPE,
    InferredCell,
    InferredGridRejection,
    InferredStructure,
    column_headers,
    inferred_header_rows,
    inferred_table,
    is_amount,
    row_label,
    structure_producer,
)
from ragspine.extraction.evidence.objects.tables.table_models import CellContentState, TableIR
from ragspine.extraction.evidence.objects.tables.table_transcription import (
    check_table_transcription,
    table_span_ids,
)

_ANCHOR = SourceAnchor("a" * 64, "a" * 64, 0, (18.0, 48.0, 382.0, 212.0))
_PRODUCER = "table-structure-tsr-v1:pdfspine/0.11.0:0123456789ab"


def _span(span_id: str, text: str, x0: float, y0: float, x1: float, y1: float) -> TextSpan:
    return TextSpan(span_id, text, (x0, y0, x1, y1), size=y1 - y0)


# A borderless statement as the text layer prints it: a two-line header (one spanning title, two
# years), an indented sub-item, accounting negatives, thousands separators, a label wrapped over
# two lines, a raised footnote marker and a total.
SPANS = (
    _span("title", "Year ended 31 December", 250.0, 54.8, 353.1, 63.8),
    _span("y24", "2024", 262.0, 68.8, 282.0, 77.8),
    _span("y23", "2023", 330.0, 68.8, 350.0, 77.8),
    _span("rev", "Revenue", 22.0, 86.8, 58.0, 95.8),
    _span("rev24", "1,234,567", 250.0, 86.8, 290.0, 95.8),
    _span("rev23", "1,100,200", 318.0, 86.8, 358.0, 95.8),
    _span("cos", "Cost of sales", 34.0, 102.8, 86.0, 111.8),
    _span("cos24", "(456,789)", 256.0, 102.8, 294.5, 111.8),
    _span("cos23", "(400,100)", 324.0, 102.8, 362.5, 111.8),
    _span("oth", "Other operating income and", 22.0, 118.8, 133.6, 127.8),
    _span("exp", "expenses", 22.0, 132.8, 60.5, 141.8),
    _span("exp24", "12,345", 268.0, 132.8, 295.5, 141.8),
    _span("exp23", "(9,876)", 330.0, 132.8, 358.5, 141.8),
    _span("op", "Operating profit", 22.0, 150.8, 84.0, 159.8),
    _span("mark", "1", 104.0, 150.0, 106.8, 155.0),
    _span("op24", "790,123", 262.0, 151.4, 294.5, 160.4),
    _span("op23", "690,224", 324.0, 150.8, 356.5, 159.8),
    _span("tot", "Total", 22.0, 170.8, 42.0, 179.8),
    _span("tot24", "790,123", 262.0, 170.8, 294.5, 179.8),
    _span("tot23", "690,224", 324.0, 170.8, 356.5, 179.8),
)

# Cell boxes as a structure model returns them (real SLANet-plus output on this statement,
# rounded): loose, overlapping their neighbours and spilling outside the region.
_ROW_BOXES = (
    (44.8, 71.0),
    (67.1, 85.7),
    (84.2, 99.8),
    (100.0, 115.3),
    (113.4, 128.7),
    (126.5, 142.5),
    (144.1, 163.9),
    (164.2, 183.3),
)
_COL_BOXES = ((14.3, 214.0), (219.7, 307.0), (300.0, 384.7))


def _box(row: int, first_col: int, last_col: int) -> tuple[float, float, float, float]:
    return (
        _COL_BOXES[first_col][0],
        _ROW_BOXES[row][0],
        _COL_BOXES[last_col][1],
        _ROW_BOXES[row][1],
    )


def _structure(*, cols: int = 3) -> InferredStructure:
    """The model's grid: row 0 is a blank stub beside the title spanning the value columns."""
    last = min(cols, 3) - 1
    cells = [
        InferredCell(0, 0, 1, 1, _box(0, 0, 0)),
        InferredCell(0, 1, 1, cols - 1, _box(0, 1, last)),
    ]
    cells.extend(
        InferredCell(row, col, 1, 1, _box(row, col, col))
        for row in range(1, len(_ROW_BOXES))
        for col in range(3)
    )
    return InferredStructure(len(_ROW_BOXES), cols, tuple(cells))


def _table(
    structure: InferredStructure | None = None, spans: tuple[TextSpan, ...] = SPANS
) -> TableIR:
    result = inferred_table(
        "table-1",
        _ANCHOR,
        _structure() if structure is None else structure,
        spans,
        producer=_PRODUCER,
    )
    assert isinstance(result, TableIR), result
    return result


def _rejection(structure: InferredStructure, spans: tuple[TextSpan, ...] = SPANS) -> str:
    result = inferred_table("table-1", _ANCHOR, structure, spans, producer=_PRODUCER)
    assert isinstance(result, InferredGridRejection), result
    return result.reason


def _cell_text(table: TableIR, row: int, col: int) -> str | None:
    (cell,) = tuple(item for item in table.cells if (item.row, item.col) == (row, col))
    return cell.text


def test_every_cell_text_is_the_text_layer_spans_own_text_and_the_grid_stays_pending() -> None:
    table = _table()
    assert table.verification is Verification.PENDING
    assert table.grid_evidence is None
    assert all(cell.verification is Verification.PENDING for cell in table.cells)
    assert (table.row_count, table.col_count) == (8, 3)
    assert _cell_text(table, 0, 1) == "Year ended 31 December"
    assert [_cell_text(table, 2, col) for col in range(3)] == ["Revenue", "1,234,567", "1,100,200"]
    assert [_cell_text(table, 3, col) for col in range(3)] == [
        "Cost of sales",
        "(456,789)",
        "(400,100)",
    ]
    # The raised footnote marker is read beside its label, never as a column of its own.
    assert _cell_text(table, 6, 0) == "Operating profit 1"
    # An empty slot is a blank cell, never a guessed value.
    blank = next(cell for cell in table.cells if (cell.row, cell.col) == (1, 0))
    assert (blank.text, blank.content_state, blank.source_span_ids) == (
        "",
        CellContentState.BLANK,
        (),
    )
    # Every span is in exactly one cell and the transcription rule of every other table holds.
    assert sorted(table_span_ids(table)) == sorted(span.span_id for span in SPANS)
    check_table_transcription(table, {span.span_id: span for span in SPANS}, anchor=_ANCHOR.bbox)


def test_coordinates_come_from_the_text_layer_so_model_box_jitter_changes_nothing() -> None:
    structure = _structure()
    jittered = replace(
        structure,
        cells=tuple(
            replace(
                cell,
                bbox=(
                    cell.bbox[0] + 0.7,
                    cell.bbox[1] - 0.3,
                    cell.bbox[2] - 0.4,
                    cell.bbox[3] + 0.2,
                ),
            )
            for cell in structure.cells
        ),
    )
    assert _table(jittered) == _table(structure)
    table = _table()
    cell = next(item for item in table.cells if (item.row, item.col) == (2, 1))
    # The cell box is the column's and the row's printed extent, inside the region.
    assert cell.bbox == (250.0, 86.8, 295.5, 95.8)
    x0, y0, x1, y1 = _ANCHOR.bbox
    assert all(
        x0 <= item.bbox[0] < item.bbox[2] <= x1 and y0 <= item.bbox[1] < item.bbox[3] <= y1
        for item in table.cells
    )


def test_the_producer_and_the_inferred_header_rows_travel_with_the_ir() -> None:
    table = _table()
    assert structure_producer(table) == _PRODUCER
    assert inferred_header_rows(table) == 2
    assert TSR_SCOPE == "tsr-inferred-grid-v1"
    assert structure_producer(replace(table, diagnostics=("pdfspine lines",))) is None


def test_inferred_headers_and_row_labels_are_read_off_the_grid_for_rendering_only() -> None:
    table = _table()
    value = next(cell for cell in table.cells if (cell.row, cell.col) == (2, 2))
    assert column_headers(table, value) == ("Year ended 31 December", "2023")
    assert row_label(table, value) == "Revenue"
    label = next(cell for cell in table.cells if (cell.row, cell.col) == (2, 0))
    assert row_label(table, label) is None
    assert column_headers(table, label) == ()


def test_a_span_no_cell_contains_falls_back() -> None:
    structure = _structure()
    narrowed = replace(
        structure,
        cells=tuple(
            replace(cell, bbox=(cell.bbox[0], cell.bbox[1], 240.0, cell.bbox[3]))
            if (cell.row, cell.col) == (2, 1)
            else cell
            for cell in structure.cells
        ),
    )
    assert _rejection(narrowed) == "span_unassigned"


def test_a_span_inside_two_cells_goes_to_the_nearer_one_and_a_true_tie_is_ambiguous() -> None:
    structure = _structure()
    # Column 1 now reaches over the whole of column 2's first value: the span lies inside both
    # boxes, and the box whose centre is nearer takes it (the model's usual overlap).
    widened = replace(
        structure,
        cells=tuple(
            replace(cell, bbox=(cell.bbox[0], cell.bbox[1], 384.7, cell.bbox[3]))
            if (cell.row, cell.col) == (2, 1)
            else cell
            for cell in structure.cells
        ),
    )
    assert _cell_text(_table(widened), 2, 2) == "1,100,200"
    assert _cell_text(_table(widened), 2, 1) == "1,234,567"
    # Two cells with the very same box: nothing tells them apart.
    same = {(cell.row, cell.col): cell.bbox for cell in structure.cells}[(2, 1)]
    twins = replace(
        structure,
        cells=tuple(
            replace(cell, bbox=same) if (cell.row, cell.col) == (2, 2) else cell
            for cell in structure.cells
        ),
    )
    assert _rejection(twins) == "span_ambiguous"


def test_an_empty_row_or_an_empty_column_falls_back() -> None:
    structure = _structure()
    extra_row = InferredStructure(
        9,
        3,
        (
            *structure.cells,
            *(
                InferredCell(8, col, 1, 1, (_COL_BOXES[col][0], 190.0, _COL_BOXES[col][1], 200.0))
                for col in range(3)
            ),
        ),
    )
    assert _rejection(extra_row) == "empty_row"
    wide = _structure(cols=4)
    extra_col = replace(
        wide,
        cells=(
            *wide.cells,
            *(
                InferredCell(row, 3, 1, 1, (385.0, _ROW_BOXES[row][0], 390.0, _ROW_BOXES[row][1]))
                for row in range(1, 8)
            ),
        ),
    )
    assert _rejection(extra_col) == "empty_column"


def test_a_grid_with_overlapping_or_missing_slots_falls_back() -> None:
    structure = _structure()
    overlapping = replace(
        structure,
        cells=(*structure.cells, InferredCell(2, 1, 1, 1, structure.cells[5].bbox)),
    )
    assert _rejection(overlapping) == "grid_invalid"
    missing = replace(
        structure,
        cells=tuple(cell for cell in structure.cells if (cell.row, cell.col) != (1, 0)),
    )
    assert _rejection(missing) == "grid_incomplete"
    assert _rejection(InferredStructure(1, 1, ())) == "grid_too_small"


def test_a_grid_whose_order_contradicts_the_printed_order_falls_back() -> None:
    structure = _structure()
    # The model's columns 1 and 2 swapped on one row: the 2023 value would sit left of 2024.
    swapped = replace(
        structure,
        cells=tuple(
            replace(cell, col=3 - cell.col) if cell.row == 2 and cell.col in (1, 2) else cell
            for cell in structure.cells
        ),
    )
    assert _rejection(swapped) == "order_inconsistent"


def test_a_header_row_must_be_recognisable() -> None:
    structure = _structure()
    # No amount anywhere: nothing tells a header from a data row.
    words = tuple(span for span in SPANS if not is_amount(span.text))
    assert _rejection(structure, words) == "no_data_row"
    # An amount in the very first row leaves no header.
    numbers_first = tuple(
        replace(span, text="9,999") if span.span_id == "title" else span for span in SPANS
    )
    assert _rejection(structure, numbers_first) == "no_header_row"


def test_amounts_and_years_are_told_apart() -> None:
    for text in ("1,234,567", "(456,789)", "-12.5", "12.5%", "(9,876)", "0", "$1,234", "\u22123"):
        assert is_amount(text), text
    for text in ("2024", "2023", "FY2024", "1H26", "Revenue", "—", "", "US$m"):
        assert not is_amount(text), text


def test_no_structure_at_all_is_a_rejection_not_an_empty_grid() -> None:
    result = inferred_table("table-1", _ANCHOR, None, SPANS, producer=_PRODUCER)
    assert isinstance(result, InferredGridRejection)
    assert result.reason == "no_structure"
    with pytest.raises(ValueError, match="span"):
        inferred_table("table-1", _ANCHOR, _structure(), (), producer=_PRODUCER)
