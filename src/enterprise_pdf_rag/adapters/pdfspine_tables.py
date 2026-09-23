"""Map pdfspine's explicit typed table slots into source-bound TableIR."""

from dataclasses import replace
from hashlib import sha256
from math import isfinite

import pdfspine

from ragspine.extraction.evidence.document.models import Bounds
from ragspine.extraction.evidence.figures.models import SourceAnchor, content_id
from ragspine.extraction.evidence.objects.tables.table_grid_proof import (
    GRID_SCOPE,
    GridRejection,
    prove_grid,
    verified_table,
)
from ragspine.extraction.evidence.objects.tables.table_models import (
    CellContentState,
    SlotState,
    TableCell,
    TableExtractionResult,
    TableIR,
    TableSlot,
)
from ragspine.extraction.evidence.page.geometry import COORDINATE_TOLERANCE, Axis, Segment
from ragspine.extraction.evidence.page.models import LayoutObject, ObjectKind, PageInput

# pdfspine's own ``find_tables(line_max_thickness=3.0)`` default: what it will not treat
# as a ruling, we do not treat as one either.
LINE_MAX_THICKNESS = 3.0


def ruling_segments(
    page: pdfspine.Page, *, line_max_thickness: float = LINE_MAX_THICKNESS
) -> tuple[Segment, ...]:
    """Axis-aligned solid strokes and thin filled rectangles from ``page.get_drawings()``.

    ``get_drawings()`` is page-top-left like text spans and ``Table.rows``/``cols``; never
    use ``get_cdrawings()`` here (PDF bottom-left). Dashed paths, diagonal lines, curves
    and strokes thicker than ``line_max_thickness`` are not rulings.
    """
    segments: list[Segment] = []
    for path_index, drawing in enumerate(page.get_drawings()):
        kind = str(drawing.get("type", ""))
        if drawing.get("dashes") not in (None, ""):
            continue
        width = float(drawing.get("width") or 0.0)
        stroked, filled = "s" in kind, "f" in kind
        for item_index, item in enumerate(drawing["items"]):
            operator = item[0]
            if operator == "l" and stroked and width <= line_max_thickness:
                (ax, ay), (bx, by) = tuple(item[1]), tuple(item[2])
                if abs(ay - by) <= COORDINATE_TOLERANCE and abs(ax - bx) > COORDINATE_TOLERANCE:
                    segments.append(
                        Segment(
                            path_index,
                            item_index,
                            "l",
                            Axis.HORIZONTAL,
                            ay,
                            min(ax, bx),
                            max(ax, bx),
                            width,
                        )
                    )
                elif abs(ax - bx) <= COORDINATE_TOLERANCE and abs(ay - by) > COORDINATE_TOLERANCE:
                    segments.append(
                        Segment(
                            path_index,
                            item_index,
                            "l",
                            Axis.VERTICAL,
                            ax,
                            min(ay, by),
                            max(ay, by),
                            width,
                        )
                    )
            elif operator == "re":
                rx0, ry0, rx1, ry1 = (float(value) for value in item[1])
                rect_width, rect_height = rx1 - rx0, ry1 - ry0
                if filled and rect_height <= line_max_thickness and rect_width > rect_height:
                    segments.append(
                        Segment(
                            path_index,
                            item_index,
                            "re-thin",
                            Axis.HORIZONTAL,
                            (ry0 + ry1) / 2,
                            rx0,
                            rx1,
                            rect_height,
                        )
                    )
                elif filled and rect_width <= line_max_thickness and rect_height > rect_width:
                    segments.append(
                        Segment(
                            path_index,
                            item_index,
                            "re-thin",
                            Axis.VERTICAL,
                            (rx0 + rx1) / 2,
                            ry0,
                            ry1,
                            rect_width,
                        )
                    )
                elif (
                    stroked
                    and width <= line_max_thickness
                    and rect_width > line_max_thickness
                    and rect_height > line_max_thickness
                ):
                    segments.extend(
                        (
                            Segment(
                                path_index,
                                item_index,
                                "re-top",
                                Axis.HORIZONTAL,
                                ry0,
                                rx0,
                                rx1,
                                width,
                            ),
                            Segment(
                                path_index,
                                item_index,
                                "re-bottom",
                                Axis.HORIZONTAL,
                                ry1,
                                rx0,
                                rx1,
                                width,
                            ),
                            Segment(
                                path_index,
                                item_index,
                                "re-left",
                                Axis.VERTICAL,
                                rx0,
                                ry0,
                                ry1,
                                width,
                            ),
                            Segment(
                                path_index,
                                item_index,
                                "re-right",
                                Axis.VERTICAL,
                                rx1,
                                ry0,
                                ry1,
                                width,
                            ),
                        )
                    )
    return tuple(segments)


def fill_rectangles(page: pdfspine.Page) -> tuple[Bounds, ...]:
    """Non-white filled rectangles (header bands); page-top-left like ``get_drawings``."""
    return tuple(
        _bounds(rectangle.rect) for rectangle in page.filled_rectangles(include_white=False)
    )


def _bounds(rect: pdfspine.Rect) -> tuple[float, float, float, float]:
    values = tuple(float(value) for value in rect)
    if len(values) != 4:
        raise ValueError("pdfspine returned invalid table bounds")
    return values[0], values[1], values[2], values[3]


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    tolerance: float = 0.5,
) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _center_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> bool:
    x = (inner[0] + inner[2]) / 2
    y = (inner[1] + inner[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


class PdfspineTableAdapter:
    """Use native structural slots; never infer a merge from legacy ``None``."""

    def extract(self, pdf: bytes, *, page: PageInput, item: LayoutObject) -> TableExtractionResult:
        self._validate_input(pdf, page=page, item=item)
        document = pdfspine.open(stream=pdf, filetype="pdf")
        try:
            if page.page_index >= document.page_count:
                raise ValueError("Table source page is absent from the PDF")
            source_page = document.load_page(page.page_index)
            if source_page.rotation != 0 or tuple(source_page.rect) != (
                0.0,
                0.0,
                page.width,
                page.height,
            ):
                raise ValueError("Table page geometry differs from PageInput")
            try:
                # ``clip`` is silently ignored by the lines strategy; the ``_contains``
                # filter below is what scopes the match to the layout region.
                detected = tuple(source_page.find_tables(strategy="lines", clip=item.bbox))
                matches = tuple(
                    table for table in detected if _contains(item.bbox, _bounds(table.bbox))
                )
            except (pdfspine.PdfError, OSError, RuntimeError, TypeError, ValueError):
                return TableExtractionResult(
                    None,
                    (
                        "pdfspine native typed-table detection failed; no grid was inferred or substituted.",
                    ),
                )
            if len(matches) != 1:
                return TableExtractionResult(
                    None,
                    (
                        f"pdfspine/{pdfspine.__version__} native lines found {len(detected)} page table(s) and {len(matches)} exact region match(es); typed table unavailable.",
                    ),
                )
            return self._map_table(matches[0], page=page, item=item, source_page=source_page)
        finally:
            document.close()

    @staticmethod
    def _validate_input(pdf: bytes, *, page: PageInput, item: LayoutObject) -> None:
        if sha256(pdf).hexdigest() != page.source_sha256:
            raise ValueError("PDF bytes do not match the PageInput source SHA-256")
        if page.page_index < 0:
            raise ValueError("Table source page index must be nonnegative")
        if item.kind is not ObjectKind.TABLE:
            raise ValueError("Table adapter requires a Table layout object")
        x0, y0, x1, y1 = item.bbox
        if not all(isfinite(value) for value in item.bbox) or not (
            0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height
        ):
            raise ValueError("Table layout bbox is outside PageInput geometry")
        observed = {span.span_id for span in page.text.spans}
        if (
            len(set(item.source_span_ids)) != len(item.source_span_ids)
            or not set(item.source_span_ids) <= observed
        ):
            raise ValueError("Table layout references unknown source occurrences")

    @staticmethod
    def _map_table(
        table: pdfspine.Table,
        *,
        page: PageInput,
        item: LayoutObject,
        source_page: pdfspine.Page,
    ) -> TableExtractionResult:
        table_bbox = _bounds(table.bbox)
        owned = set(item.source_span_ids)
        spans_in_table = tuple(
            span for span in page.text.spans if _center_inside(span.bbox, table_bbox)
        )
        if any(span.span_id not in owned for span in spans_in_table):
            return TableExtractionResult(
                None,
                (
                    "Native table contains a source occurrence not assigned to the Table layout object.",
                ),
            )
        cells: list[TableCell] = []
        cell_ids: dict[tuple[int, int], str] = {}
        for native in table.origin_cells:
            bbox = _bounds(native.bbox)
            source_span_ids = tuple(
                span.span_id for span in spans_in_table if _center_inside(span.bbox, bbox)
            )
            if native.state == "present" and not source_span_ids:
                return TableExtractionResult(
                    None,
                    (
                        "Native table returned present cell text without a source text occurrence; typed table withheld.",
                    ),
                )
            state = CellContentState(native.state)
            cell_id = content_id(
                "table-cell-v1",
                (
                    item.object_id,
                    native.row,
                    native.col,
                    native.row_span,
                    native.col_span,
                    bbox,
                    native.text,
                    state,
                    source_span_ids,
                ),
            )
            cell_ids[(native.row, native.col)] = cell_id
            cells.append(
                TableCell(
                    cell_id,
                    native.row,
                    native.col,
                    native.row_span,
                    native.col_span,
                    bbox,
                    source_span_ids,
                    native.text,
                    state,
                )
            )
        slots = tuple(
            tuple(
                TableSlot(
                    SlotState.UNKNOWN
                    if native.cell is None
                    else (
                        SlotState.CONTINUATION
                        if native.state == "continuation"
                        else SlotState.ORIGIN
                    ),
                    None if native.origin is None else cell_ids.get(native.origin),
                )
                for native in row
            )
            for row in table.slots
        )
        diagnostics: tuple[str, ...] = (
            f"pdfspine/{pdfspine.__version__} typed slots; source={table.source}; text_source={table.text_source}; confidence={'unknown' if table.confidence is None else table.confidence}.",
            "Cell source occurrences use bbox-center assignment; legacy extract() None values were not used.",
        )
        pending = TableIR(
            item.object_id,
            SourceAnchor(
                page.source_sha256,
                page.source_sha256,
                page.page_index,
                table_bbox,
            ),
            table.row_count,
            table.col_count,
            tuple(cells),
            slots,
            diagnostics=diagnostics,
        )
        segments = ruling_segments(source_page)
        proof = prove_grid(
            pending,
            segments,
            rows=table.rows,
            cols=table.cols,
            fills=fill_rectangles(source_page),
            spans=spans_in_table,
        )
        if isinstance(proof, GridRejection):
            diagnostics = (
                *diagnostics,
                f"Grid structure pending: {proof.reason} (rulings={len(segments)}).",
            )
            return TableExtractionResult(replace(pending, diagnostics=diagnostics), diagnostics)
        diagnostics = (
            *diagnostics,
            f"Grid structure proved from {len(segments)} ruling segment(s); scope={GRID_SCOPE}.",
        )
        return TableExtractionResult(
            verified_table(replace(pending, diagnostics=diagnostics), proof), diagnostics
        )
