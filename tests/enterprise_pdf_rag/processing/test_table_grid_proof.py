"""A table grid is verified only when every boundary, edge and merge cites a real ruling."""

from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.tables.table_grid_proof import (
    GridProof,
    GridRejection,
    check_grid_evidence,
    prove_grid,
    strip_grid_evidence,
    verified_table,
)
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellContentState,
    HeaderEvidenceKind,
    HeaderStrength,
    SlotState,
    TableCell,
    TableIR,
    TableSlot,
)
from ragspine.extraction.evidence.page.geometry import Axis, Segment

ROWS = (20.0, 70.0, 120.0)
COLS = (20.0, 120.0, 220.0)
THREE_ROWS = (20.0, 70.0, 120.0, 170.0)
PAGE_HEIGHT = 180.0


def _horizontal(
    path_index: int, y: float, start: float, end: float, thickness: float = 1.0
) -> Segment:
    return Segment(path_index, 0, "l", Axis.HORIZONTAL, y, start, end, thickness)


def _vertical(
    path_index: int, x: float, start: float, end: float, thickness: float = 1.0
) -> Segment:
    return Segment(path_index, 0, "l", Axis.VERTICAL, x, start, end, thickness)


def _full_segments(
    *,
    rows: tuple[float, ...] = ROWS,
    cols: tuple[float, ...] = COLS,
    thickness: Mapping[int, float] | None = None,
) -> tuple[Segment, ...]:
    widths = dict(thickness or {})
    return (
        *(
            _horizontal(index, y, cols[0], cols[-1], widths.get(index, 1.0))
            for index, y in enumerate(rows)
        ),
        *(_vertical(len(rows) + index, x, rows[0], rows[-1]) for index, x in enumerate(cols)),
    )


def _table(
    *,
    rows: tuple[float, ...] = ROWS,
    cols: tuple[float, ...] = COLS,
    merges: Mapping[tuple[int, int], tuple[int, int]] | None = None,
    unknown: tuple[tuple[int, int], ...] = (),
) -> TableIR:
    spans = dict(merges or {})
    row_count, col_count = len(rows) - 1, len(cols) - 1
    covered: dict[tuple[int, int], tuple[SlotState, str]] = {}
    cells: list[TableCell] = []
    for row in range(row_count):
        for col in range(col_count):
            if (row, col) in covered or (row, col) in unknown:
                continue
            row_span, col_span = spans.get((row, col), (1, 1))
            cell_id = f"cell-{row}-{col}"
            cells.append(
                TableCell(
                    cell_id,
                    row,
                    col,
                    row_span,
                    col_span,
                    (cols[col], rows[row], cols[col + col_span], rows[row + row_span]),
                    (f"span-{row}-{col}",),
                    f"value {row}{col}",
                    CellContentState.PRESENT,
                )
            )
            for spanned_row in range(row, row + row_span):
                for spanned_col in range(col, col + col_span):
                    covered[(spanned_row, spanned_col)] = (
                        SlotState.ORIGIN
                        if (spanned_row, spanned_col) == (row, col)
                        else SlotState.CONTINUATION,
                        cell_id,
                    )
    slots = tuple(
        tuple(
            TableSlot(*covered[(row, col)])
            if (row, col) in covered
            else TableSlot(SlotState.UNKNOWN, None)
            for col in range(col_count)
        )
        for row in range(row_count)
    )
    return TableIR(
        "table-object",
        SourceAnchor("manifest", "a" * 64, 2, (cols[0], rows[0], cols[-1], rows[-1])),
        row_count,
        col_count,
        tuple(cells),
        slots,
    )


def _proof(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    fills: Sequence[Bounds] = (),
    spans: Sequence[TextSpan] = (),
) -> GridProof:
    proof = prove_grid(table, segments, rows=rows, cols=cols, fills=fills, spans=spans)
    assert isinstance(proof, GridProof)
    return proof


def _rejection(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
) -> GridRejection:
    proof = prove_grid(table, segments, rows=rows, cols=cols)
    assert isinstance(proof, GridRejection)
    return proof


def test_fully_ruled_2x2_is_proved_with_four_borders_per_cell() -> None:
    table = _table()
    proof = _proof(table, _full_segments(), rows=ROWS, cols=COLS)

    assert set(proof.borders) == {cell.cell_id for cell in table.cells}
    for border in proof.borders.values():
        assert (len(border.top), len(border.bottom), len(border.left), len(border.right)) == (
            1,
            1,
            1,
            1,
        )
        assert border.merge_proof is None
    assert proof.evidence.rows == ROWS
    assert proof.evidence.cols == COLS
    assert proof.evidence.segment_count == 6
    assert proof.evidence.producer == "ruled-grid-structure-v1"

    verified = verified_table(table, proof)
    assert verified.verification is Verification.VERIFIED
    assert all(cell.verification is Verification.VERIFIED for cell in verified.cells)
    assert tuple(cell.cell_id for cell in verified.cells) == tuple(
        cell.cell_id for cell in table.cells
    )
    assert strip_grid_evidence(verified) == table


def test_boundary_without_ruling_is_rejected() -> None:
    segments = tuple(
        segment
        for segment in _full_segments()
        if not (segment.axis is Axis.HORIZONTAL and segment.position == ROWS[1])
    )
    rejection = _rejection(_table(), segments, rows=ROWS, cols=COLS)
    assert "row boundary 1" in rejection.reason
    assert rejection.cell_id is None


def test_snapped_boundary_is_rejected() -> None:
    snapped = (20.0, 71.0, 120.0)
    segments = (
        _horizontal(0, 20.0, 20.0, 220.0),
        _horizontal(1, 70.0, 20.0, 120.0),
        _horizontal(2, 72.0, 120.0, 220.0),
        _horizontal(3, 120.0, 20.0, 220.0),
        _vertical(4, 20.0, 20.0, 120.0),
        _vertical(5, 120.0, 20.0, 120.0),
        _vertical(6, 220.0, 20.0, 120.0),
    )
    rejection = _rejection(_table(rows=snapped), segments, rows=snapped, cols=COLS)
    assert "row boundary 1" in rejection.reason


def test_edge_not_continuously_ruled_is_rejected() -> None:
    segments = (
        _horizontal(0, 20.0, 20.0, 100.0),
        _horizontal(1, 20.0, 101.0, 220.0),
        _horizontal(2, 70.0, 20.0, 220.0),
        _horizontal(3, 120.0, 20.0, 220.0),
        _vertical(4, 20.0, 20.0, 120.0),
        _vertical(5, 120.0, 20.0, 120.0),
        _vertical(6, 220.0, 20.0, 120.0),
    )
    rejection = _rejection(_table(), segments, rows=ROWS, cols=COLS)
    assert "top edge is not continuously ruled" in rejection.reason
    assert rejection.cell_id == "cell-0-0"


def test_edge_covered_by_stitched_pieces_passes() -> None:
    segments = (
        _horizontal(0, 20.0, 20.0, 100.0),
        _horizontal(1, 20.0, 100.3, 220.0),
        _horizontal(2, 70.0, 20.0, 220.0),
        _horizontal(3, 120.0, 20.0, 220.0),
        _vertical(4, 20.0, 20.0, 120.0),
        _vertical(5, 120.0, 20.0, 120.0),
        _vertical(6, 220.0, 20.0, 120.0),
    )
    proof = _proof(_table(), segments, rows=ROWS, cols=COLS)
    assert len(proof.borders["cell-0-0"].top) == 2
    assert len(proof.borders["cell-0-1"].top) == 1


def test_row_col_index_misaligned_with_boundaries_is_rejected() -> None:
    cells = (
        TableCell(
            "top", 0, 0, 1, 1, (20.0, 20.0, 120.0, 70.0), ("span-a",), "a", CellContentState.PRESENT
        ),
        TableCell(
            "displaced",
            1,
            0,
            1,
            1,
            (20.0, 20.0, 120.0, 70.0),
            ("span-b",),
            "b",
            CellContentState.PRESENT,
        ),
    )
    table = TableIR(
        "table-object",
        SourceAnchor("manifest", "a" * 64, 2, (20.0, 20.0, 120.0, 120.0)),
        2,
        1,
        cells,
        ((TableSlot(SlotState.ORIGIN, "top"),), (TableSlot(SlotState.ORIGIN, "displaced"),)),
    )
    narrow = (20.0, 120.0)
    segments = _full_segments(rows=ROWS, cols=narrow)
    rejection = _rejection(table, segments, rows=ROWS, cols=narrow)
    assert "not aligned" in rejection.reason
    assert rejection.cell_id == "displaced"


def test_merged_cell_requires_missing_interior_ruling() -> None:
    table = _table(merges={(0, 0): (2, 1)})
    base = (
        _horizontal(0, 20.0, 20.0, 220.0),
        _horizontal(1, 120.0, 20.0, 220.0),
        _vertical(2, 20.0, 20.0, 120.0),
        _vertical(3, 120.0, 20.0, 120.0),
        _vertical(4, 220.0, 20.0, 120.0),
    )
    proof = _proof(table, (*base, _horizontal(5, 70.0, 120.0, 220.0)), rows=ROWS, cols=COLS)
    merge_proof = proof.borders["cell-0-0"].merge_proof
    assert merge_proof is not None
    assert (merge_proof.interior_rows, merge_proof.interior_cols) == ((1,), ())
    assert proof.borders["cell-0-1"].merge_proof is None

    crossed = (*base, _horizontal(5, 70.0, 120.0, 220.0), _horizontal(6, 70.0, 40.0, 80.0))
    rejection = _rejection(table, crossed, rows=ROWS, cols=COLS)
    assert "interior ruling at row boundary 1" in rejection.reason
    assert rejection.cell_id == "cell-0-0"

    touching = (*base, _horizontal(5, 70.0, 119.6, 220.0))
    assert _proof(table, touching, rows=ROWS, cols=COLS).borders["cell-0-0"].merge_proof is not None


def test_unknown_slot_is_rejected() -> None:
    rejection = _rejection(_table(unknown=((1, 1),)), _full_segments(), rows=ROWS, cols=COLS)
    assert "unknown slots" in rejection.reason


def test_thick_rule_header_is_proved_but_first_row_is_heuristic() -> None:
    table = _table(rows=THREE_ROWS)
    thick = _full_segments(rows=THREE_ROWS, thickness={1: 2.0})
    headers = _proof(table, thick, rows=THREE_ROWS, cols=COLS).evidence

    assert (HeaderEvidenceKind.RULING_THICK, HeaderStrength.PROVED, (0,)) in {
        (header.kind, header.strength, header.rows) for header in headers.headers
    }
    assert (HeaderEvidenceKind.FIRST_ROW_RULE, HeaderStrength.HEURISTIC, (0,)) in {
        (header.kind, header.strength, header.rows) for header in headers.headers
    }
    assert headers.proved_header_rows() == frozenset({0})
    assert headers.proved_header_cols() == frozenset()

    uniform = _proof(table, _full_segments(rows=THREE_ROWS), rows=THREE_ROWS, cols=COLS).evidence
    assert {header.kind for header in uniform.headers} == {HeaderEvidenceKind.FIRST_ROW_RULE}
    assert uniform.proved_header_rows() == frozenset()


def test_bold_font_header_stays_heuristic() -> None:
    table = _table()
    spans = tuple(
        TextSpan(f"span-0-{col}", "H", (0.0, 0.0, 1.0, 1.0), font="Helvetica-bold")
        for col in range(2)
    ) + tuple(
        TextSpan(f"span-1-{col}", "v", (0.0, 0.0, 1.0, 1.0), font="Helvetica") for col in range(2)
    )
    evidence = _proof(table, _full_segments(), rows=ROWS, cols=COLS, spans=spans).evidence
    bold = tuple(
        header for header in evidence.headers if header.kind is HeaderEvidenceKind.FONT_BOLD
    )
    assert len(bold) == 1
    assert (bold[0].strength, bold[0].rows) == (HeaderStrength.HEURISTIC, (0,))
    assert evidence.proved_header_rows() == frozenset()


def test_fill_header_band_is_proved() -> None:
    table = _table()
    band: Bounds = (COLS[0], ROWS[0], COLS[-1], ROWS[1])
    evidence = _proof(table, _full_segments(), rows=ROWS, cols=COLS, fills=(band,)).evidence
    (fill,) = tuple(header for header in evidence.headers if header.kind is HeaderEvidenceKind.FILL)
    assert (fill.strength, fill.rows, fill.cols, fill.fills) == (
        HeaderStrength.PROVED,
        (0,),
        (),
        (band,),
    )
    assert evidence.proved_header_rows() == frozenset({0})

    whole: Bounds = (COLS[0], ROWS[0], COLS[-1], ROWS[-1])
    covered = _proof(table, _full_segments(), rows=ROWS, cols=COLS, fills=(whole,)).evidence
    assert all(header.kind is not HeaderEvidenceKind.FILL for header in covered.headers)


def test_bottom_left_coordinates_do_not_prove() -> None:
    flipped = tuple(
        Segment(
            segment.path_index,
            segment.item_index,
            segment.edge,
            segment.axis,
            PAGE_HEIGHT - segment.position,
            segment.start,
            segment.end,
            segment.thickness,
        )
        if segment.axis is Axis.HORIZONTAL
        else Segment(
            segment.path_index,
            segment.item_index,
            segment.edge,
            segment.axis,
            segment.position,
            PAGE_HEIGHT - segment.end,
            PAGE_HEIGHT - segment.start,
            segment.thickness,
        )
        for segment in _full_segments()
    )
    rejection = _rejection(_table(), flipped, rows=ROWS, cols=COLS)
    assert "row boundary 0" in rejection.reason


def test_check_grid_evidence_reproves_and_detects_tampering() -> None:
    table = _table()
    segments = _full_segments()
    verified = verified_table(table, _proof(table, segments, rows=ROWS, cols=COLS))

    check_grid_evidence(verified, segments)
    check_grid_evidence(strip_grid_evidence(verified), ())

    with pytest.raises(ValueError, match="does not re-prove"):
        check_grid_evidence(verified, segments[:-1])
    assert verified.grid_evidence is not None
    with pytest.raises(ValueError, match="differs from the re-proved grid"):
        check_grid_evidence(
            replace(
                verified, grid_evidence=replace(verified.grid_evidence, ruling_digest="c" * 64)
            ),
            segments,
        )
    with pytest.raises(ValueError, match="aligned"):
        replace(verified, grid_evidence=replace(verified.grid_evidence, rows=(20.0, 70.4, 120.0)))


def test_prove_grid_refuses_an_already_proved_table() -> None:
    table = _table()
    verified = verified_table(table, _proof(table, _full_segments(), rows=ROWS, cols=COLS))
    with pytest.raises(ValueError, match="unproved table"):
        prove_grid(verified, _full_segments(), rows=ROWS, cols=COLS)
