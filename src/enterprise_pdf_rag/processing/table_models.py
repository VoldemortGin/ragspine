"""Immutable table grids that keep structure uncertainty distinct from blank cells."""

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from math import isfinite

from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor, Verification
from enterprise_pdf_rag.processing.geometry import COORDINATE_TOLERANCE


class CellContentState(StrEnum):
    PRESENT = "present"
    BLANK = "blank"
    UNAVAILABLE = "unavailable"


class SlotState(StrEnum):
    ORIGIN = "origin"
    CONTINUATION = "continuation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SegmentRef:
    """A ruling piece in ``page.get_drawings()``: which path/item/edge, and its endpoints."""

    path_index: int
    item_index: int
    edge: str
    p0: tuple[float, float]
    p1: tuple[float, float]
    thickness: float

    def __post_init__(self) -> None:
        if self.path_index < 0 or self.item_index < 0 or not self.edge or self.thickness < 0:
            raise ValueError(
                "Segment reference must name a drawing path and a non-negative thickness"
            )
        if self.p0 == self.p1 or not all(isfinite(value) for value in (*self.p0, *self.p1)):
            raise ValueError("Segment reference must have two distinct finite endpoints")


@dataclass(frozen=True, slots=True)
class MergeProof:
    """Interior grid boundaries a merged cell spans, each proved free of rulings inside it."""

    interior_rows: tuple[int, ...]
    interior_cols: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CellBorderEvidence:
    top: tuple[SegmentRef, ...]
    bottom: tuple[SegmentRef, ...]
    left: tuple[SegmentRef, ...]
    right: tuple[SegmentRef, ...]
    merge_proof: MergeProof | None = None

    def __post_init__(self) -> None:
        if not (self.top and self.bottom and self.left and self.right):
            raise ValueError("Every cell edge needs at least one ruling segment")


class HeaderEvidenceKind(StrEnum):
    RULING_THICK = "ruling_thick"
    FILL = "fill"
    FONT_BOLD = "font_bold"
    FIRST_ROW_RULE = "first_row_rule"


class HeaderStrength(StrEnum):
    PROVED = "proved"
    HEURISTIC = "heuristic"


@dataclass(frozen=True, slots=True)
class HeaderEvidence:
    kind: HeaderEvidenceKind
    strength: HeaderStrength
    rows: tuple[int, ...]
    cols: tuple[int, ...]
    segments: tuple[SegmentRef, ...] = ()
    fills: tuple[Bounds, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.rows) == bool(self.cols):
            raise ValueError(
                "Header evidence names header rows or header columns, not both or neither"
            )
        if self.kind in (
            HeaderEvidenceKind.FONT_BOLD,
            HeaderEvidenceKind.FIRST_ROW_RULE,
        ) and (self.strength is not HeaderStrength.HEURISTIC):
            raise ValueError("Font and first-row header hints are heuristics, never proofs")
        if self.kind is HeaderEvidenceKind.RULING_THICK and (
            self.strength is HeaderStrength.PROVED and not self.segments
        ):
            raise ValueError("A proved thick-rule header cites its ruling segments")
        if (
            self.kind is HeaderEvidenceKind.FILL
            and self.strength is HeaderStrength.PROVED
            and not self.fills
        ):
            raise ValueError("A proved fill header cites its filled rectangles")


@dataclass(frozen=True, slots=True)
class GridEvidence:
    """Why this grid is a source fact: every boundary is a real ruling in the page drawings."""

    rows: tuple[float, ...]
    cols: tuple[float, ...]
    ruling_digest: str
    segment_count: int
    tolerance: float
    headers: tuple[HeaderEvidence, ...] = ()
    producer: str = "ruled-grid-structure-v1"

    def __post_init__(self) -> None:
        for values in (self.rows, self.cols):
            if len(values) < 2 or any(second - first <= 0 for first, second in pairwise(values)):
                raise ValueError("Grid boundaries must be strictly increasing")
        if len(self.ruling_digest) != 64 or self.segment_count < 1 or self.tolerance <= 0:
            raise ValueError(
                "Grid evidence must bind a ruling digest, a segment count and a tolerance"
            )

    def proved_header_rows(self) -> frozenset[int]:
        return frozenset(
            row
            for header in self.headers
            if header.strength is HeaderStrength.PROVED
            for row in header.rows
        )

    def proved_header_cols(self) -> frozenset[int]:
        return frozenset(
            col
            for header in self.headers
            if header.strength is HeaderStrength.PROVED
            for col in header.cols
        )


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
    border: CellBorderEvidence | None = None

    def __post_init__(self) -> None:
        if self.verification is Verification.REJECTED:
            raise ValueError("Inferred table cells are pending or verified, never rejected")
        if (self.verification is Verification.VERIFIED) != (self.border is not None):
            raise ValueError(
                "Inferred table cells must remain pending unless a producer supplies border evidence"
            )
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
        has_origin = self.origin_cell_id is not None and bool(self.origin_cell_id.strip())
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
    grid_evidence: GridEvidence | None = None

    def __post_init__(self) -> None:
        if (self.verification is Verification.VERIFIED) != (self.grid_evidence is not None):
            raise ValueError(
                "Inferred table IR must remain pending unless a producer supplies grid evidence"
            )
        if any(cell.verification is not self.verification for cell in self.cells):
            raise ValueError("Table cells share the table's grid verification")
        if (
            self.row_count < 1
            or self.col_count < 1
            or len(self.slots) != self.row_count
            or any(len(row) != self.col_count for row in self.slots)
        ):
            raise ValueError("Table slots must match rectangular dimensions")
        cell_ids = tuple(cell.cell_id for cell in self.cells)
        if any(not cell_id.strip() for cell_id in cell_ids) or len(set(cell_ids)) != len(cell_ids):
            raise ValueError("Table grid cell IDs must be non-empty and unique")
        source_ids = tuple(span_id for cell in self.cells for span_id in cell.source_span_ids)
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
                        raise ValueError("Table grid references a cell outside its declared spans")
                elif (slot.state, slot.origin_cell_id) != expected_slot:
                    raise ValueError("Table grid slots must match each cell origin and span")
        if self.grid_evidence is not None:
            self._check_grid_evidence()

    def _check_grid_evidence(self) -> None:
        """Structural consistency of the stored evidence; ``table_grid_proof`` owns the geometry."""
        evidence = self.grid_evidence
        if evidence is None:
            raise ValueError("Grid evidence check requires evidence")
        rows, cols = evidence.rows, evidence.cols
        if len(rows) != self.row_count + 1 or len(cols) != self.col_count + 1:
            raise ValueError("Grid evidence boundaries do not match the table dimensions")
        x0, y0, x1, y1 = self.source.bbox
        if any(
            abs(first - second) > COORDINATE_TOLERANCE
            for first, second in ((cols[0], x0), (rows[0], y0), (cols[-1], x1), (rows[-1], y1))
        ):
            raise ValueError("Grid evidence boundaries do not match the table bbox")
        for cell in self.cells:
            cell_x0, cell_y0, cell_x1, cell_y1 = cell.bbox
            expected = (
                cols[cell.col],
                rows[cell.row],
                cols[cell.col + cell.col_span],
                rows[cell.row + cell.row_span],
            )
            if any(
                abs(first - second) > COORDINATE_TOLERANCE
                for first, second in zip(cell.bbox, expected, strict=True)
            ):
                raise ValueError("Table cell bbox is not aligned to the proved grid boundaries")
            border = cell.border
            if border is None:
                raise ValueError("A verified table cell carries border evidence")
            for refs, edge_position in (
                (border.top, cell_y0),
                (border.bottom, cell_y1),
                (border.left, cell_x0),
                (border.right, cell_x1),
            ):
                for ref in refs:
                    horizontal = ref.p0[1] == ref.p1[1]
                    position = ref.p0[1] if horizontal else ref.p0[0]
                    if abs(position - edge_position) > evidence.tolerance:
                        raise ValueError("Cell border evidence does not lie on the cell edge")
            spans_more = cell.row_span > 1 or cell.col_span > 1
            if spans_more != (border.merge_proof is not None):
                raise ValueError("Merged cells carry a merge proof; single cells do not")
            if border.merge_proof is not None and (
                border.merge_proof.interior_rows
                != tuple(range(cell.row + 1, cell.row + cell.row_span))
                or border.merge_proof.interior_cols
                != tuple(range(cell.col + 1, cell.col + cell.col_span))
            ):
                raise ValueError(
                    "Merge proof must name exactly the interior boundaries the cell spans"
                )
        for header in evidence.headers:
            if any(row >= self.row_count for row in header.rows) or any(
                col >= self.col_count for col in header.cols
            ):
                raise ValueError("Header evidence names rows or columns outside the grid")
        for slots in self.slots:
            if any(slot.state is SlotState.UNKNOWN for slot in slots):
                raise ValueError("A verified grid has no unknown slots")


@dataclass(frozen=True, slots=True)
class TableExtractionResult:
    table: TableIR | None
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.table is None and not self.diagnostics:
            raise ValueError("Unavailable table extraction requires a diagnostic")
        if any(not diagnostic.strip() for diagnostic in self.diagnostics):
            raise ValueError("Table extraction diagnostics must be non-empty")
