"""Immutable table grids that keep structure uncertainty distinct from blank cells."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor, Verification


class CellContentState(StrEnum):
    PRESENT = "present"
    BLANK = "blank"
    UNAVAILABLE = "unavailable"


class SlotState(StrEnum):
    ORIGIN = "origin"
    CONTINUATION = "continuation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TableCell:
    cell_id: str
    row: int
    col: int
    row_span: int
    col_span: int
    bbox: Bounds
    source_span_ids: tuple[str, ...]
    text: str | None
    content_state: CellContentState
    verification: Verification = Verification.PENDING

    def __post_init__(self) -> None:
        if self.verification is not Verification.PENDING:
            raise ValueError("Inferred table cells must remain pending")
        if any(not value for value in self.source_span_ids) or len(
            set(self.source_span_ids)
        ) != len(self.source_span_ids):
            raise ValueError("Cell source span IDs must be non-empty and unique")
        x0, y0, x1, y1 = self.bbox
        if (
            self.row < 0
            or self.col < 0
            or self.row_span < 1
            or self.col_span < 1
            or not all(isfinite(value) for value in self.bbox)
            or x0 >= x1
            or y0 >= y1
        ):
            raise ValueError("Cell origin, span and bbox must be valid")
        content_is_valid = (
            self.content_state is CellContentState.PRESENT
            and self.text is not None
            and bool(self.text.strip())
        ) or (self.content_state is CellContentState.BLANK and self.text == "")
        content_is_valid = content_is_valid or (
            self.content_state is CellContentState.UNAVAILABLE and self.text is None
        )
        if not content_is_valid:
            raise ValueError("Cell text must match its content state")


@dataclass(frozen=True, slots=True)
class TableSlot:
    state: SlotState
    origin_cell_id: str | None

    def __post_init__(self) -> None:
        has_origin = self.origin_cell_id is not None and bool(
            self.origin_cell_id.strip()
        )
        if (self.state is SlotState.UNKNOWN and self.origin_cell_id is not None) or (
            self.state is not SlotState.UNKNOWN and not has_origin
        ):
            raise ValueError("Origin cell ID must match the slot state")


@dataclass(frozen=True, slots=True)
class TableIR:
    object_id: str
    source: SourceAnchor
    row_count: int
    col_count: int
    cells: tuple[TableCell, ...]
    slots: tuple[tuple[TableSlot, ...], ...]
    verification: Verification = Verification.PENDING
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.verification is not Verification.PENDING or any(
            cell.verification is not Verification.PENDING for cell in self.cells
        ):
            raise ValueError("Inferred table IR must remain pending")
        if (
            self.row_count < 1
            or self.col_count < 1
            or len(self.slots) != self.row_count
            or any(len(row) != self.col_count for row in self.slots)
        ):
            raise ValueError("Table slots must match rectangular dimensions")
        cell_ids = tuple(cell.cell_id for cell in self.cells)
        if any(not cell_id.strip() for cell_id in cell_ids) or len(
            set(cell_ids)
        ) != len(cell_ids):
            raise ValueError("Table grid cell IDs must be non-empty and unique")
        source_ids = tuple(
            span_id for cell in self.cells for span_id in cell.source_span_ids
        )
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("A source occurrence cannot belong to multiple cells")
        expected: dict[tuple[int, int], tuple[SlotState, str]] = {}
        table_x0, table_y0, table_x1, table_y1 = self.source.bbox
        for cell in self.cells:
            cell_x0, cell_y0, cell_x1, cell_y1 = cell.bbox
            if not (
                table_x0 <= cell_x0 < cell_x1 <= table_x1
                and table_y0 <= cell_y0 < cell_y1 <= table_y1
            ):
                raise ValueError("Table cell must remain inside the source bbox")
            if (
                cell.row + cell.row_span > self.row_count
                or cell.col + cell.col_span > self.col_count
            ):
                raise ValueError("Table grid cell span exceeds its dimensions")
            for row in range(cell.row, cell.row + cell.row_span):
                for col in range(cell.col, cell.col + cell.col_span):
                    coordinate = (row, col)
                    if coordinate in expected:
                        raise ValueError("Table grid cells must not overlap")
                    state = (
                        SlotState.ORIGIN
                        if coordinate == (cell.row, cell.col)
                        else SlotState.CONTINUATION
                    )
                    expected[coordinate] = (state, cell.cell_id)
        for row, slots in enumerate(self.slots):
            for col, slot in enumerate(slots):
                expected_slot = expected.get((row, col))
                if expected_slot is None:
                    if slot.state is not SlotState.UNKNOWN:
                        raise ValueError(
                            "Table grid references a cell outside its declared spans"
                        )
                elif (slot.state, slot.origin_cell_id) != expected_slot:
                    raise ValueError(
                        "Table grid slots must match each cell origin and span"
                    )


@dataclass(frozen=True, slots=True)
class TableExtractionResult:
    table: TableIR | None
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.table is None and not self.diagnostics:
            raise ValueError("Unavailable table extraction requires a diagnostic")
        if any(not diagnostic.strip() for diagnostic in self.diagnostics):
            raise ValueError("Table extraction diagnostics must be non-empty")
