"""A table grid inferred by a structure model, filled from the text layer, kept PENDING (enterprise-pdf-rag ADR 0031).

A region with no ruled grid (ADR 0014 proves nothing there) can still be given rows and
columns by a table-structure model. That grid is the model's assertion, so it never becomes
evidence of a row / column / header relation: the result is an ordinary *pending*
``TableIR`` whose every cell text is the page's own spans, assigned by span centre.

The model contributes the **discrete** structure only — how many rows and columns, which slot
each cell occupies and spans, and therefore which spans share a cell. Every coordinate of the
stored IR is re-read from the text layer (a cell box is its column's and its row's printed
extent), so float noise in the model's boxes cannot change the IR; only a span whose centre
moves across a cell boundary can. Producer and validator share ``inferred_table``, so a grid
admitted at processing time must re-derive identically from the same model at resolve time.

The self-check refuses (with a reason code) rather than repairs: a grid with a missing or
overlapping slot, a span no cell (or two cells equally) contains, an empty row or column, a
cell order that contradicts the printed order, or no recognisable header row. A span whose
centre two overlapping boxes hold goes to the one covering more of it, then to the nearer
box; only a full tie is ambiguous.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import hypot

from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor, content_id
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellContentState,
    SlotState,
    TableCell,
    TableIR,
    TableSlot,
)
from ragspine.extraction.evidence.objects.tables.table_rows import group_rows

# The receipt scope of a table whose grid a structure model inferred; distinct from the
# literal scope of a pdfspine-detected grid and from ADR 0027's verbatim rows.
TSR_SCOPE = "tsr-inferred-grid-v1"
# The rule version; the full producer appends the parser and the model weights' digest.
TSR_PRODUCER = "table-structure-tsr-v1"
# Prefix of the diagnostic a table falling back to verbatim rows records: ``<prefix>:<reason>``.
TSR_FALLBACK = "tsr_fallback"
_PRODUCER_PREFIX = "structure_producer="
_HEADER_PREFIX = "inferred_header_rows="
_NOT_PROVED = (
    "Rows, columns, merges and headers are inferred by a table-structure model and are not "
    "proved; every cell text is the text layer's own spans."
)

# An amount as a financial table prints it: an optional sign, accounting brackets, a currency
# symbol, thousands separators, decimals and a percent sign. A bare four-digit year is a
# period label, not an amount; so are ``FY2024`` / ``1H26`` (they do not match at all).
_AMOUNT = re.compile(
    r"[(]?\s*[-+\u2212\u2013]?\s*[$\u20ac\u00a3\u00a5]?\s*"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*%?\s*[)]?"
)
_YEAR = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True, slots=True)
class InferredCell:
    """One cell as the model places it: its slot, its spans and its (model) box."""

    row: int
    col: int
    row_span: int
    col_span: int
    bbox: Bounds


@dataclass(frozen=True, slots=True)
class InferredStructure:
    """A model's whole grid for one region; boxes are page-top-left points."""

    row_count: int
    col_count: int
    cells: tuple[InferredCell, ...]


@dataclass(frozen=True, slots=True)
class InferredGridRejection:
    """Why an inferred grid was not kept: a stable code plus a human detail."""

    reason: str
    detail: str


def is_amount(text: str) -> bool:
    """Whether ``text`` is printed as an amount (not a year, a label or a placeholder)."""
    value = text.strip()
    return bool(value) and _AMOUNT.fullmatch(value) is not None and _YEAR.fullmatch(value) is None


def _center(bbox: Bounds) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def _inside(point: tuple[float, float], bbox: Bounds) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _overlap(first: Bounds, second: Bounds) -> float:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return max(width, 0.0) * max(height, 0.0)


def _check_slots(structure: InferredStructure) -> InferredGridRejection | None:
    if structure.row_count < 2 or structure.col_count < 2 or not structure.cells:
        return InferredGridRejection(
            "grid_too_small",
            f"{structure.row_count} row(s) x {structure.col_count} column(s)",
        )
    taken: set[tuple[int, int]] = set()
    for cell in structure.cells:
        if (
            cell.row < 0
            or cell.col < 0
            or cell.row_span < 1
            or cell.col_span < 1
            or cell.row + cell.row_span > structure.row_count
            or cell.col + cell.col_span > structure.col_count
        ):
            return InferredGridRejection("grid_invalid", f"cell at ({cell.row},{cell.col})")
        for row in range(cell.row, cell.row + cell.row_span):
            for col in range(cell.col, cell.col + cell.col_span):
                if (row, col) in taken:
                    return InferredGridRejection("grid_invalid", f"slot ({row},{col}) twice")
                taken.add((row, col))
    if len(taken) != structure.row_count * structure.col_count:
        return InferredGridRejection(
            "grid_incomplete",
            f"{structure.row_count * structure.col_count - len(taken)} slot(s) uncovered",
        )
    return None


def _assign(
    cells: Sequence[InferredCell], spans: Sequence[TextSpan]
) -> dict[int, list[TextSpan]] | InferredGridRejection:
    """Each span to the one cell holding its centre.

    A model's boxes overlap their neighbours, so a centre inside two boxes goes to the box
    covering more of the span, then to the box whose own centre is nearer; only a full tie
    (the same box twice) is ambiguous.
    """
    owned: dict[int, list[TextSpan]] = {}
    for span in spans:
        center = _center(span.bbox)
        found = [index for index, cell in enumerate(cells) if _inside(center, cell.bbox)]
        if not found:
            return InferredGridRejection("span_unassigned", f"span {span.span_id}")
        if len(found) > 1:
            ranked = sorted(
                (
                    -_overlap(span.bbox, cells[index].bbox),
                    hypot(
                        _center(cells[index].bbox)[0] - center[0],
                        _center(cells[index].bbox)[1] - center[1],
                    ),
                    index,
                )
                for index in found
            )
            if ranked[0][:2] == ranked[1][:2]:
                return InferredGridRejection("span_ambiguous", f"span {span.span_id}")
            found = [ranked[0][2]]
        owned.setdefault(found[0], []).append(span)
    return owned


def _empty(
    structure: InferredStructure, owned: dict[int, list[TextSpan]]
) -> InferredGridRejection | None:
    """Every row and column prints text in a cell of its own, not only under a spanning one."""
    cells = [structure.cells[index] for index in owned]
    rows = {cell.row for cell in cells if cell.row_span == 1}
    cols = {cell.col for cell in cells if cell.col_span == 1}
    for row in range(structure.row_count):
        if row not in rows:
            return InferredGridRejection("empty_row", f"row {row}")
    for col in range(structure.col_count):
        if col not in cols:
            return InferredGridRejection("empty_column", f"column {col}")
    return None


def _ordered(
    structure: InferredStructure, owned: dict[int, list[TextSpan]]
) -> InferredGridRejection | None:
    """A cell left of (above) another in the grid prints all its spans left of (above) it."""
    cells = structure.cells
    centers = {index: tuple(_center(span.bbox) for span in spans) for index, spans in owned.items()}
    for first in sorted(owned):
        a = cells[first]
        for second in sorted(owned):
            b = cells[second]
            rows_meet = a.row < b.row + b.row_span and b.row < a.row + a.row_span
            cols_meet = a.col < b.col + b.col_span and b.col < a.col + a.col_span
            left_of = rows_meet and a.col + a.col_span <= b.col
            if left_of and max(x for x, _ in centers[first]) >= min(x for x, _ in centers[second]):
                return InferredGridRejection(
                    "order_inconsistent", f"columns {a.col} and {b.col} in row {a.row}"
                )
            above = cols_meet and a.row + a.row_span <= b.row
            if above and max(y for _, y in centers[first]) >= min(y for _, y in centers[second]):
                return InferredGridRejection(
                    "order_inconsistent", f"rows {a.row} and {b.row} in column {a.col}"
                )
    return None


def _band(
    structure: InferredStructure,
    owned: dict[int, list[TextSpan]],
    index: int,
    *,
    axis: str,
) -> tuple[float, float]:
    """The printed extent of one row (``axis="row"``) or column, from its single-slot cells."""
    found: list[TextSpan] = []
    for position, spans in owned.items():
        cell = structure.cells[position]
        start, size = (cell.row, cell.row_span) if axis == "row" else (cell.col, cell.col_span)
        if (start, size) == (index, 1):
            found.extend(spans)
    low, high = (1, 3) if axis == "row" else (0, 2)
    return min(span.bbox[low] for span in found), max(span.bbox[high] for span in found)


def inferred_table(
    object_id: str,
    source: SourceAnchor,
    structure: InferredStructure | None,
    spans: Sequence[TextSpan],
    *,
    producer: str,
) -> TableIR | InferredGridRejection:
    """The pending ``TableIR`` of ``structure`` filled with ``spans`` (the region's own, page order).

    Returns a rejection when the model gave no structure or the structure fails the
    self-check; raises ``ValueError`` only for an empty region.
    """
    if not spans:
        raise ValueError("No source span is available for this table region")
    if structure is None:
        return InferredGridRejection("no_structure", "the structure model returned no grid")
    invalid = _check_slots(structure)
    if invalid is not None:
        return invalid
    owned = _assign(structure.cells, spans)
    if isinstance(owned, InferredGridRejection):
        return owned
    for check in (_empty(structure, owned), _ordered(structure, owned)):
        if check is not None:
            return check
    rows = tuple(_band(structure, owned, row, axis="row") for row in range(structure.row_count))
    cols = tuple(_band(structure, owned, col, axis="col") for col in range(structure.col_count))
    ordered = sorted(
        range(len(structure.cells)), key=lambda i: (structure.cells[i].row, structure.cells[i].col)
    )
    cells: list[TableCell] = []
    for index in ordered:
        model = structure.cells[index]
        lines = group_rows(owned.get(index, ()))
        members = tuple(span for line in lines for span in line)
        text = "\n".join(" ".join(span.text for span in line) for line in lines)
        bounds = [
            min(cols[c][0] for c in range(model.col, model.col + model.col_span)),
            min(rows[r][0] for r in range(model.row, model.row + model.row_span)),
            max(cols[c][1] for c in range(model.col, model.col + model.col_span)),
            max(rows[r][1] for r in range(model.row, model.row + model.row_span)),
        ]
        for span in members:
            bounds = [
                min(bounds[0], span.bbox[0]),
                min(bounds[1], span.bbox[1]),
                max(bounds[2], span.bbox[2]),
                max(bounds[3], span.bbox[3]),
            ]
        bbox = (bounds[0], bounds[1], bounds[2], bounds[3])
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            return InferredGridRejection(
                "degenerate_geometry", f"cell at ({model.row},{model.col})"
            )
        state = CellContentState.PRESENT if members else CellContentState.BLANK
        span_ids = tuple(span.span_id for span in members)
        cells.append(
            TableCell(
                content_id(
                    "tsr-table-cell-v1",
                    (
                        object_id,
                        model.row,
                        model.col,
                        model.row_span,
                        model.col_span,
                        bbox,
                        text,
                        state,
                        span_ids,
                    ),
                ),
                model.row,
                model.col,
                model.row_span,
                model.col_span,
                bbox,
                span_ids,
                text,
                state,
            )
        )
    slot_of: dict[tuple[int, int], TableSlot] = {}
    for cell in cells:
        for row in range(cell.row, cell.row + cell.row_span):
            for col in range(cell.col, cell.col + cell.col_span):
                slot_of[(row, col)] = TableSlot(
                    SlotState.ORIGIN
                    if (row, col) == (cell.row, cell.col)
                    else SlotState.CONTINUATION,
                    cell.cell_id,
                )
    draft = TableIR(
        object_id,
        source,
        structure.row_count,
        structure.col_count,
        tuple(cells),
        tuple(
            tuple(slot_of[(row, col)] for col in range(structure.col_count))
            for row in range(structure.row_count)
        ),
    )
    headers = inferred_header_rows(draft)
    if headers == 0:
        return InferredGridRejection("no_header_row", "the first row already prints an amount")
    if headers >= draft.row_count:
        return InferredGridRejection("no_data_row", "no row prints an amount")
    return TableIR(
        draft.object_id,
        draft.source,
        draft.row_count,
        draft.col_count,
        draft.cells,
        draft.slots,
        diagnostics=(
            _PRODUCER_PREFIX + producer,
            f"{_HEADER_PREFIX}{headers}",
            _NOT_PROVED,
        ),
    )


def inferred_header_rows(table: TableIR) -> int:
    """The leading rows printing no amount outside the stub column: the inferred header."""
    for row in range(table.row_count):
        if any(
            cell.col >= 1
            and cell.row <= row < cell.row + cell.row_span
            and cell.text is not None
            and is_amount(cell.text)
            for cell in table.cells
        ):
            return row
    return table.row_count


def structure_producer(table: TableIR) -> str | None:
    """The structure producer an inferred grid records, or ``None`` for any other grid."""
    if table.diagnostics and table.diagnostics[0].startswith(_PRODUCER_PREFIX):
        return table.diagnostics[0].removeprefix(_PRODUCER_PREFIX)
    return None


def column_headers(table: TableIR, cell: TableCell) -> tuple[str, ...]:
    """The inferred header texts above ``cell``'s columns, top down; for rendering only."""
    rows = inferred_header_rows(table)
    return tuple(
        header.text
        for header in sorted(table.cells, key=lambda item: (item.row, item.col))
        if header.cell_id != cell.cell_id
        and header.row < rows
        and header.row < cell.row
        and header.text
        and header.col < cell.col + cell.col_span
        and cell.col < header.col + header.col_span
    )


def row_label(table: TableIR, cell: TableCell) -> str | None:
    """The stub (first-column) text on ``cell``'s row, when ``cell`` is a value beside it."""
    if cell.col == 0:
        return None
    for label in table.cells:
        if label.col == 0 and label.row <= cell.row < label.row + label.row_span and label.text:
            return label.text
    return None
