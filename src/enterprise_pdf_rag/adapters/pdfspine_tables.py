"""Map pdfspine's explicit typed table slots into source-bound TableIR."""

from hashlib import sha256
from math import isfinite

import pdfspine

from enterprise_pdf_rag.figures.models import SourceAnchor, content_id
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.table_models import (
    CellContentState,
    SlotState,
    TableCell,
    TableExtractionResult,
    TableIR,
    TableSlot,
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
            return self._map_table(matches[0], page=page, item=item)
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
        table: pdfspine.Table, *, page: PageInput, item: LayoutObject
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
        diagnostics = (
            f"pdfspine/{pdfspine.__version__} typed slots; source={table.source}; text_source={table.text_source}; confidence={'unknown' if table.confidence is None else table.confidence}.",
            "Cell source occurrences use bbox-center assignment and remain pending; legacy extract() None values were not used.",
        )
        return TableExtractionResult(
            TableIR(
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
            ),
            diagnostics,
        )
