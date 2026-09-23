"""Typed table grids preserve blank, unavailable, merge and unknown semantics."""

import pytest

from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellBorderEvidence,
    CellContentState,
    GridEvidence,
    MergeProof,
    SegmentRef,
    SlotState,
    TableCell,
    TableIR,
    TableSlot,
)


def _source() -> SourceAnchor:
    return SourceAnchor("manifest", "a" * 64, 19, (0.0, 0.0, 300.0, 200.0))


def test_grid_preserves_merged_blank_unavailable_and_unknown_slots() -> None:
    cells = (
        TableCell(
            "header",
            0,
            0,
            1,
            2,
            (0.0, 0.0, 200.0, 100.0),
            ("span-header",),
            "Sensitivity",
            CellContentState.PRESENT,
        ),
        TableCell(
            "blank",
            1,
            0,
            1,
            1,
            (0.0, 100.0, 100.0, 200.0),
            (),
            "",
            CellContentState.BLANK,
        ),
        TableCell(
            "value",
            1,
            1,
            1,
            1,
            (100.0, 100.0, 200.0, 200.0),
            ("span-value",),
            "10",
            CellContentState.PRESENT,
        ),
        TableCell(
            "unread",
            1,
            2,
            1,
            1,
            (200.0, 100.0, 300.0, 200.0),
            (),
            None,
            CellContentState.UNAVAILABLE,
        ),
    )
    table = TableIR(
        "table-p20-sensitivity",
        _source(),
        2,
        3,
        cells,
        (
            (
                TableSlot(SlotState.ORIGIN, "header"),
                TableSlot(SlotState.CONTINUATION, "header"),
                TableSlot(SlotState.UNKNOWN, None),
            ),
            (
                TableSlot(SlotState.ORIGIN, "blank"),
                TableSlot(SlotState.ORIGIN, "value"),
                TableSlot(SlotState.ORIGIN, "unread"),
            ),
        ),
    )

    assert table.verification is Verification.PENDING
    assert table.cells[1].content_state is CellContentState.BLANK
    assert table.cells[3].content_state is CellContentState.UNAVAILABLE
    assert table.slots[0][1].state is SlotState.CONTINUATION
    assert table.slots[0][2].state is SlotState.UNKNOWN


@pytest.mark.parametrize(
    ("state", "text"),
    (
        (CellContentState.PRESENT, ""),
        (CellContentState.PRESENT, None),
        (CellContentState.BLANK, None),
        (CellContentState.BLANK, "not blank"),
        (CellContentState.UNAVAILABLE, ""),
        (CellContentState.UNAVAILABLE, "observed"),
    ),
)
def test_cell_content_state_rejects_text_conflation(
    state: CellContentState, text: str | None
) -> None:
    with pytest.raises(ValueError, match="content state"):
        TableCell(
            "cell",
            0,
            0,
            1,
            1,
            (0.0, 0.0, 10.0, 10.0),
            (),
            text,
            state,
        )


@pytest.mark.parametrize(
    ("row", "col", "row_span", "col_span", "bbox"),
    (
        (-1, 0, 1, 1, (0.0, 0.0, 10.0, 10.0)),
        (0, -1, 1, 1, (0.0, 0.0, 10.0, 10.0)),
        (0, 0, 0, 1, (0.0, 0.0, 10.0, 10.0)),
        (0, 0, 1, 0, (0.0, 0.0, 10.0, 10.0)),
        (0, 0, 1, 1, (0.0, 0.0, float("nan"), 10.0)),
        (0, 0, 1, 1, (10.0, 0.0, 0.0, 10.0)),
    ),
)
def test_cell_rejects_invalid_origin_span_or_bbox(
    row: int,
    col: int,
    row_span: int,
    col_span: int,
    bbox: tuple[float, float, float, float],
) -> None:
    with pytest.raises(ValueError, match="origin, span and bbox"):
        TableCell(
            "cell",
            row,
            col,
            row_span,
            col_span,
            bbox,
            ("span",),
            "value",
            CellContentState.PRESENT,
        )


@pytest.mark.parametrize(
    ("state", "origin_cell_id"),
    (
        (SlotState.ORIGIN, None),
        (SlotState.ORIGIN, ""),
        (SlotState.CONTINUATION, None),
        (SlotState.UNKNOWN, "cell"),
    ),
)
def test_slot_state_rejects_ambiguous_origin(state: SlotState, origin_cell_id: str | None) -> None:
    with pytest.raises(ValueError, match="slot state"):
        TableSlot(state, origin_cell_id)


def test_table_rejects_non_rectangular_slot_dimensions() -> None:
    cell = TableCell(
        "cell",
        0,
        0,
        1,
        1,
        (0.0, 0.0, 10.0, 10.0),
        ("span",),
        "value",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="rectangular dimensions"):
        TableIR(
            "table",
            _source(),
            2,
            2,
            (cell,),
            ((TableSlot(SlotState.ORIGIN, "cell"),),),
        )


def test_table_rejects_missing_origin_and_wrong_continuation() -> None:
    merged = TableCell(
        "merged",
        0,
        0,
        1,
        2,
        (0.0, 0.0, 20.0, 10.0),
        ("span",),
        "value",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="grid"):
        TableIR(
            "table",
            _source(),
            1,
            2,
            (merged,),
            (
                (
                    TableSlot(SlotState.CONTINUATION, "merged"),
                    TableSlot(SlotState.CONTINUATION, "merged"),
                ),
            ),
        )
    with pytest.raises(ValueError, match="grid"):
        TableIR(
            "table",
            _source(),
            1,
            2,
            (merged,),
            (
                (
                    TableSlot(SlotState.ORIGIN, "merged"),
                    TableSlot(SlotState.CONTINUATION, "other"),
                ),
            ),
        )


def _ruled_border(
    *,
    top_y: float,
    bottom_y: float,
    left_x: float = 0.0,
    right_x: float = 10.0,
    merge_proof: MergeProof | None = None,
) -> CellBorderEvidence:
    return CellBorderEvidence(
        (SegmentRef(0, 0, "l", (left_x, top_y), (right_x, top_y), 1.0),),
        (SegmentRef(1, 0, "l", (left_x, bottom_y), (right_x, bottom_y), 1.0),),
        (SegmentRef(2, 0, "l", (left_x, top_y), (left_x, bottom_y), 1.0),),
        (SegmentRef(3, 0, "l", (right_x, top_y), (right_x, bottom_y), 1.0),),
        merge_proof,
    )


def _evidence(*, rows: tuple[float, ...], cols: tuple[float, ...]) -> GridEvidence:
    return GridEvidence(rows, cols, "b" * 64, 4, 0.5)


def _stacked_cell(cell_id: str, row: int, *, border: CellBorderEvidence) -> TableCell:
    top = 10.0 * row
    return TableCell(
        cell_id,
        row,
        0,
        1,
        1,
        (0.0, top, 10.0, top + 10.0),
        (f"span-{row}",),
        "value",
        CellContentState.PRESENT,
        Verification.VERIFIED,
        border,
    )


def test_table_without_grid_evidence_stays_pending() -> None:
    with pytest.raises(ValueError, match="border evidence"):
        TableCell(
            "cell",
            0,
            0,
            1,
            1,
            (0.0, 0.0, 10.0, 10.0),
            ("span",),
            "value",
            CellContentState.PRESENT,
            Verification.VERIFIED,
        )
    cell = TableCell(
        "cell",
        0,
        0,
        1,
        1,
        (0.0, 0.0, 10.0, 10.0),
        ("span",),
        "value",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="grid evidence"):
        TableIR(
            "table",
            _source(),
            1,
            1,
            (cell,),
            ((TableSlot(SlotState.ORIGIN, "cell"),),),
            Verification.VERIFIED,
        )
    with pytest.raises(ValueError, match="grid evidence"):
        TableIR(
            "table",
            _source(),
            1,
            1,
            (cell,),
            ((TableSlot(SlotState.ORIGIN, "cell"),),),
            grid_evidence=_evidence(rows=(0.0, 10.0), cols=(0.0, 10.0)),
        )


def test_table_with_grid_evidence_is_verified() -> None:
    anchor = SourceAnchor("manifest", "a" * 64, 19, (0.0, 0.0, 10.0, 20.0))
    slots = (
        (TableSlot(SlotState.ORIGIN, "top"),),
        (TableSlot(SlotState.ORIGIN, "bottom"),),
    )
    cells = (
        _stacked_cell("top", 0, border=_ruled_border(top_y=0.0, bottom_y=10.0)),
        _stacked_cell("bottom", 1, border=_ruled_border(top_y=10.0, bottom_y=20.0)),
    )
    boundaries = _evidence(rows=(0.0, 10.0, 20.0), cols=(0.0, 10.0))
    table = TableIR(
        "table", anchor, 2, 1, cells, slots, Verification.VERIFIED, grid_evidence=boundaries
    )
    assert table.verification is Verification.VERIFIED
    assert all(cell.verification is Verification.VERIFIED for cell in table.cells)

    with pytest.raises(ValueError, match="aligned"):
        TableIR(
            "table",
            anchor,
            2,
            1,
            cells,
            slots,
            Verification.VERIFIED,
            grid_evidence=_evidence(rows=(0.0, 11.0, 20.0), cols=(0.0, 10.0)),
        )
    displaced = (
        _stacked_cell("top", 0, border=_ruled_border(top_y=3.0, bottom_y=10.0)),
        cells[1],
    )
    with pytest.raises(ValueError, match="lie on the cell edge"):
        TableIR(
            "table", anchor, 2, 1, displaced, slots, Verification.VERIFIED, grid_evidence=boundaries
        )
    merged = (
        _stacked_cell(
            "top", 0, border=_ruled_border(top_y=0.0, bottom_y=10.0, merge_proof=MergeProof((), ()))
        ),
        cells[1],
    )
    with pytest.raises(ValueError, match="merge proof"):
        TableIR(
            "table", anchor, 2, 1, merged, slots, Verification.VERIFIED, grid_evidence=boundaries
        )


def test_table_rejects_cell_outside_source_bbox() -> None:
    cell = TableCell(
        "cell",
        0,
        0,
        1,
        1,
        (290.0, 190.0, 310.0, 210.0),
        ("span",),
        "value",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="source bbox"):
        TableIR(
            "table",
            _source(),
            1,
            1,
            (cell,),
            ((TableSlot(SlotState.ORIGIN, "cell"),),),
        )


def test_cell_rejects_duplicate_or_empty_source_occurrence_ids() -> None:
    for source_ids in (("span", "span"), ("",)):
        with pytest.raises(ValueError, match="source span IDs"):
            TableCell(
                "cell",
                0,
                0,
                1,
                1,
                (0.0, 0.0, 10.0, 10.0),
                source_ids,
                "value",
                CellContentState.PRESENT,
            )


def test_rowspan_and_colspan_require_every_continuation_to_name_origin() -> None:
    merged = TableCell(
        "merged",
        0,
        0,
        2,
        2,
        (0.0, 0.0, 200.0, 200.0),
        ("span",),
        "value",
        CellContentState.PRESENT,
    )
    table = TableIR(
        "table",
        _source(),
        3,
        3,
        (merged,),
        (
            (
                TableSlot(SlotState.ORIGIN, "merged"),
                TableSlot(SlotState.CONTINUATION, "merged"),
                TableSlot(SlotState.UNKNOWN, None),
            ),
            (
                TableSlot(SlotState.CONTINUATION, "merged"),
                TableSlot(SlotState.CONTINUATION, "merged"),
                TableSlot(SlotState.UNKNOWN, None),
            ),
            (
                TableSlot(SlotState.UNKNOWN, None),
                TableSlot(SlotState.UNKNOWN, None),
                TableSlot(SlotState.UNKNOWN, None),
            ),
        ),
    )
    assert table.slots[1][1].origin_cell_id == "merged"

    with pytest.raises(ValueError, match="grid"):
        TableIR(
            "table",
            _source(),
            2,
            2,
            (merged,),
            (
                (
                    TableSlot(SlotState.ORIGIN, "merged"),
                    TableSlot(SlotState.CONTINUATION, "merged"),
                ),
                (
                    TableSlot(SlotState.CONTINUATION, "merged"),
                    TableSlot(SlotState.UNKNOWN, None),
                ),
            ),
        )


def test_overlapping_cells_are_rejected_even_when_slots_name_one_cell() -> None:
    wide = TableCell(
        "wide",
        0,
        0,
        1,
        2,
        (0.0, 0.0, 20.0, 10.0),
        (),
        "wide",
        CellContentState.PRESENT,
    )
    overlap = TableCell(
        "overlap",
        0,
        1,
        1,
        1,
        (10.0, 0.0, 20.0, 10.0),
        (),
        "overlap",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="overlap"):
        TableIR(
            "table",
            _source(),
            1,
            2,
            (wide, overlap),
            (
                (
                    TableSlot(SlotState.ORIGIN, "wide"),
                    TableSlot(SlotState.ORIGIN, "overlap"),
                ),
            ),
        )


def test_one_source_occurrence_cannot_be_assigned_to_two_cells() -> None:
    left = TableCell(
        "left",
        0,
        0,
        1,
        1,
        (0.0, 0.0, 10.0, 10.0),
        ("same-span",),
        "left",
        CellContentState.PRESENT,
    )
    right = TableCell(
        "right",
        0,
        1,
        1,
        1,
        (10.0, 0.0, 20.0, 10.0),
        ("same-span",),
        "right",
        CellContentState.PRESENT,
    )
    with pytest.raises(ValueError, match="source occurrence"):
        TableIR(
            "table",
            _source(),
            1,
            2,
            (left, right),
            (
                (
                    TableSlot(SlotState.ORIGIN, "left"),
                    TableSlot(SlotState.ORIGIN, "right"),
                ),
            ),
        )
