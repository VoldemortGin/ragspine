"""Prove a native table's grid from the page's rulings; anything unproved stays pending.

A boundary counts only when a real axis-aligned ruling lies on it; a cell edge counts
only when rulings cover it end to end; a merge counts only when the interior
boundaries it spans carry no ruling inside the cell. Nothing here reads text.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from types import MappingProxyType

from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellBorderEvidence,
    CellContentState,
    GridEvidence,
    HeaderEvidence,
    HeaderEvidenceKind,
    HeaderStrength,
    MergeProof,
    SegmentRef,
    SlotState,
    TableIR,
)
from ragspine.extraction.evidence.page.geometry import (
    COORDINATE_TOLERANCE,
    RULING_TOLERANCE,
    Axis,
    Segment,
    contains,
    covering_segments,
    ruling_digest,
    rulings_at,
    segments_crossing,
)

GRID_SCOPE = "ruled-grid-structure-v1"


@dataclass(frozen=True, slots=True)
class GridRejection:
    reason: str
    cell_id: str | None = None


@dataclass(frozen=True, slots=True)
class GridProof:
    """A proved grid: the evidence to pin on the IR plus one border per cell.

    ``borders`` is a read-only view over the proof's own mapping, so a proof cannot be
    edited between ``prove_grid`` and ``verified_table``.
    """

    evidence: GridEvidence
    borders: Mapping[str, CellBorderEvidence]


def segment_ref(segment: Segment) -> SegmentRef:
    """The stored reference back to the drawing path this ruling piece came from."""
    if segment.axis is Axis.HORIZONTAL:
        p0, p1 = (segment.start, segment.position), (segment.end, segment.position)
    else:
        p0, p1 = (segment.position, segment.start), (segment.position, segment.end)
    return SegmentRef(
        segment.path_index, segment.item_index, segment.edge, p0, p1, segment.thickness
    )


def strip_grid_evidence(table: TableIR) -> TableIR:
    """The same grid as an unproved observation (validators re-prove from this)."""
    return replace(
        table,
        verification=Verification.PENDING,
        grid_evidence=None,
        cells=tuple(
            replace(cell, verification=Verification.PENDING, border=None) for cell in table.cells
        ),
    )


def prove_grid(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    fills: Sequence[Bounds] = (),
    spans: Sequence[TextSpan] = (),
    tolerance: float = RULING_TOLERANCE,
) -> GridProof | GridRejection:
    """Bind every boundary, edge and merge of ``table`` to rulings, or say why not.

    ``rows`` / ``cols`` are pdfspine's snapped boundaries (``Table.rows`` / ``Table.cols``);
    they are inputs to be proved, not trusted. ``fills`` (filled rectangles) and ``spans``
    only feed header evidence. The table must be pending and carry no evidence.
    """
    if table.verification is not Verification.PENDING or table.grid_evidence is not None:
        raise ValueError("prove_grid takes an unproved table")
    row_values = tuple(float(value) for value in rows)
    col_values = tuple(float(value) for value in cols)
    if len(row_values) != table.row_count + 1 or len(col_values) != table.col_count + 1:
        return GridRejection("grid boundary count does not match the table dimensions")
    if any(
        second - first <= tolerance
        for values in (row_values, col_values)
        for first, second in pairwise(values)
    ):
        return GridRejection("grid boundaries are not strictly increasing beyond the tolerance")
    x0, y0, x1, y1 = table.source.bbox
    if any(
        abs(first - second) > COORDINATE_TOLERANCE
        for first, second in (
            (col_values[0], x0),
            (row_values[0], y0),
            (col_values[-1], x1),
            (row_values[-1], y1),
        )
    ):
        return GridRejection("grid boundaries do not match the table bbox")
    if any(slot.state is SlotState.UNKNOWN for line in table.slots for slot in line):
        return GridRejection("grid has unknown slots; an irregular grid is not proved")
    for index, y in enumerate(row_values):
        if not rulings_at(segments, Axis.HORIZONTAL, y, tolerance=tolerance):
            return GridRejection(
                f"row boundary {index} at y={y!r} has no ruling within {tolerance}pt"
            )
    for index, x in enumerate(col_values):
        if not rulings_at(segments, Axis.VERTICAL, x, tolerance=tolerance):
            return GridRejection(
                f"column boundary {index} at x={x!r} has no ruling within {tolerance}pt"
            )
    borders: dict[str, CellBorderEvidence] = {}
    for cell in table.cells:
        cell_x0, cell_y0, cell_x1, cell_y1 = cell.bbox
        expected = (
            col_values[cell.col],
            row_values[cell.row],
            col_values[cell.col + cell.col_span],
            row_values[cell.row + cell.row_span],
        )
        if any(
            abs(first - second) > COORDINATE_TOLERANCE
            for first, second in zip(cell.bbox, expected, strict=True)
        ):
            return GridRejection("cell bbox is not aligned to the grid boundaries", cell.cell_id)
        edges: list[tuple[SegmentRef, ...]] = []
        for name, axis, position, start, end in (
            ("top", Axis.HORIZONTAL, cell_y0, cell_x0, cell_x1),
            ("bottom", Axis.HORIZONTAL, cell_y1, cell_x0, cell_x1),
            ("left", Axis.VERTICAL, cell_x0, cell_y0, cell_y1),
            ("right", Axis.VERTICAL, cell_x1, cell_y0, cell_y1),
        ):
            covered = covering_segments(segments, axis, position, start, end, tolerance=tolerance)
            if covered is None:
                return GridRejection(
                    f"cell ({cell.row},{cell.col}) {name} edge is not continuously ruled",
                    cell.cell_id,
                )
            edges.append(tuple(segment_ref(piece) for piece in covered))
        interior_rows = tuple(range(cell.row + 1, cell.row + cell.row_span))
        interior_cols = tuple(range(cell.col + 1, cell.col + cell.col_span))
        for index in interior_rows:
            if segments_crossing(
                segments, Axis.HORIZONTAL, row_values[index], cell_x0, cell_x1, tolerance=tolerance
            ):
                return GridRejection(
                    f"merged cell ({cell.row},{cell.col}) has an interior ruling at row boundary {index}",
                    cell.cell_id,
                )
        for index in interior_cols:
            if segments_crossing(
                segments, Axis.VERTICAL, col_values[index], cell_y0, cell_y1, tolerance=tolerance
            ):
                return GridRejection(
                    f"merged cell ({cell.row},{cell.col}) has an interior ruling at column boundary {index}",
                    cell.cell_id,
                )
        top, bottom, left, right = edges
        borders[cell.cell_id] = CellBorderEvidence(
            top,
            bottom,
            left,
            right,
            MergeProof(interior_rows, interior_cols) if (interior_rows or interior_cols) else None,
        )
    evidence = GridEvidence(
        row_values,
        col_values,
        ruling_digest(segments),
        len(segments),
        tolerance,
        header_evidence(
            table,
            segments,
            rows=row_values,
            cols=col_values,
            fills=fills,
            spans=spans,
            tolerance=tolerance,
        ),
    )
    return GridProof(evidence, MappingProxyType(borders))


def verified_table(table: TableIR, proof: GridProof) -> TableIR:
    """Attach the proof; cell ids are untouched (``content_id`` never included verification)."""
    if set(proof.borders) != {cell.cell_id for cell in table.cells}:
        raise ValueError("Grid proof does not cover exactly the table's cells")
    return replace(
        table,
        verification=Verification.VERIFIED,
        grid_evidence=proof.evidence,
        cells=tuple(
            replace(cell, verification=Verification.VERIFIED, border=proof.borders[cell.cell_id])
            for cell in table.cells
        ),
    )


def check_grid_evidence(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    fills: Sequence[Bounds] = (),
    spans: Sequence[TextSpan] = (),
) -> None:
    """Raise unless ``table``'s stored evidence re-proves from ``segments`` (validator side)."""
    evidence = table.grid_evidence
    if table.verification is Verification.PENDING or evidence is None:
        return
    pending = strip_grid_evidence(table)
    proof = prove_grid(
        pending,
        segments,
        rows=evidence.rows,
        cols=evidence.cols,
        fills=fills,
        spans=spans,
        tolerance=evidence.tolerance,
    )
    if isinstance(proof, GridRejection):
        raise ValueError(
            f"Table grid evidence does not re-prove from the pinned source: {proof.reason}"
        )
    if verified_table(pending, proof) != table:
        raise ValueError("Table grid evidence differs from the re-proved grid")


def header_evidence(
    table: TableIR,
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    fills: Sequence[Bounds] = (),
    spans: Sequence[TextSpan] = (),
    tolerance: float = RULING_TOLERANCE,
) -> tuple[HeaderEvidence, ...]:
    """Header rows/columns: thick rules and fills prove one, fonts and position only hint."""
    found: list[HeaderEvidence] = []
    found.extend(_thick_rule_headers(segments, rows=rows, cols=cols, tolerance=tolerance))
    found.extend(_fill_headers(table, rows=rows, cols=cols, fills=fills, tolerance=tolerance))
    bold = _bold_font_header(table, spans=spans)
    if bold is not None:
        found.append(bold)
    if table.row_count >= 2 and any(
        cell.row == 0 and cell.content_state is CellContentState.PRESENT for cell in table.cells
    ):
        found.append(
            HeaderEvidence(HeaderEvidenceKind.FIRST_ROW_RULE, HeaderStrength.HEURISTIC, (0,), ())
        )
    return tuple(found)


def _thick_rule_headers(
    segments: Sequence[Segment],
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    tolerance: float,
) -> list[HeaderEvidence]:
    """An interior boundary ruled thicker than every other interior one closes the header."""
    found: list[HeaderEvidence] = []
    for axis, boundaries, start, end, along_rows in (
        (Axis.HORIZONTAL, rows, cols[0], cols[-1], True),
        (Axis.VERTICAL, cols, rows[0], rows[-1], False),
    ):
        interior = range(1, len(boundaries) - 1)
        for index in interior:
            covered = covering_segments(
                segments, axis, boundaries[index], start, end, tolerance=tolerance
            )
            if covered is None:
                continue
            own = min(piece.thickness for piece in covered)
            others = [
                piece.thickness
                for other in interior
                if other != index
                for piece in rulings_at(segments, axis, boundaries[other], tolerance=tolerance)
            ]
            if others and own - max(others) > tolerance:
                found.append(
                    HeaderEvidence(
                        HeaderEvidenceKind.RULING_THICK,
                        HeaderStrength.PROVED,
                        tuple(range(index)) if along_rows else (),
                        () if along_rows else tuple(range(index)),
                        tuple(segment_ref(piece) for piece in covered),
                    )
                )
                break
    return found


def _fill_headers(
    table: TableIR,
    *,
    rows: Sequence[float],
    cols: Sequence[float],
    fills: Sequence[Bounds],
    tolerance: float,
) -> list[HeaderEvidence]:
    """Leading full-width (or full-height) bands covered by a filled rectangle are a header."""
    found: list[HeaderEvidence] = []
    for count, boundaries, along_rows in (
        (table.row_count, rows, True),
        (table.col_count, cols, False),
    ):
        matched: list[Bounds] = []
        for index in range(count):
            band: Bounds = (
                (cols[0], boundaries[index], cols[-1], boundaries[index + 1])
                if along_rows
                else (boundaries[index], rows[0], boundaries[index + 1], rows[-1])
            )
            fill = next(
                (
                    rectangle
                    for rectangle in fills
                    if contains(rectangle, band, tolerance=tolerance)
                ),
                None,
            )
            if fill is None:
                break
            matched.append(fill)
        if 0 < len(matched) < count:
            found.append(
                HeaderEvidence(
                    HeaderEvidenceKind.FILL,
                    HeaderStrength.PROVED,
                    tuple(range(len(matched))) if along_rows else (),
                    () if along_rows else tuple(range(len(matched))),
                    (),
                    tuple(matched),
                )
            )
    return found


def _bold_font_header(table: TableIR, *, spans: Sequence[TextSpan]) -> HeaderEvidence | None:
    """Leading rows whose every present occurrence is set in a bold face; a hint, never a proof."""
    fonts = {span.span_id: span.font for span in spans}

    def bold_row(row: int) -> bool:
        occurrences = tuple(
            span_id
            for cell in table.cells
            if cell.row == row and cell.content_state is CellContentState.PRESENT
            for span_id in cell.source_span_ids
        )
        return bool(occurrences) and all(
            "bold" in fonts.get(span_id, "").casefold() for span_id in occurrences
        )

    row = 0
    while row < table.row_count and bold_row(row):
        row += 1
    if 0 < row < table.row_count:
        return HeaderEvidence(
            HeaderEvidenceKind.FONT_BOLD, HeaderStrength.HEURISTIC, tuple(range(row)), ()
        )
    return None
