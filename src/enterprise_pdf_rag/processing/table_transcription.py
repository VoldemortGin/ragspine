"""Literal table transcription: every cell must repeat its source occurrences verbatim.

The grid itself (rows, columns, merges) is inferred and stays ``PENDING``; what a
verified table member qualifies is only that each present cell's text is an exact
copy of the page's text occurrences it owns, so a cell citation always resolves to
real spans. Nothing here derives, merges or reorders a value.
"""

from collections.abc import Mapping

from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.table_models import CellContentState, TableIR


def table_span_ids(table: TableIR) -> tuple[str, ...]:
    """Source occurrences in cell order; this is also the transcription's line order."""
    return tuple(span_id for cell in table.cells for span_id in cell.source_span_ids)


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _center_inside(outer: Bounds, inner: Bounds) -> bool:
    x = (inner[0] + inner[2]) / 2
    y = (inner[1] + inner[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def check_table_transcription(
    table: TableIR, spans: Mapping[str, TextSpan], *, anchor: Bounds
) -> None:
    """Raise ``ValueError`` unless the grid and every cell are exact source transcriptions.

    Producer and validator share this rule so a table admitted at processing time
    always re-verifies at index and resolve time.
    """
    if not contains(anchor, table.source.bbox):
        raise ValueError("Table grid is outside its qualified anchor")
    for cell in table.cells:
        observed: list[str] = []
        for span_id in cell.source_span_ids:
            span = spans.get(span_id)
            if span is None:
                raise ValueError("Table cell references an unknown source occurrence")
            if not contains(anchor, span.bbox):
                raise ValueError("Table source occurrence is outside its qualified anchor")
            if not _center_inside(cell.bbox, span.bbox):
                raise ValueError("Table source occurrence is outside its cell")
            observed.append(span.text)
        if cell.content_state is CellContentState.PRESENT:
            if (
                cell.text is None
                or not observed
                or _collapsed(cell.text) != _collapsed(" ".join(observed))
            ):
                raise ValueError(
                    "Table cell text is not an exact transcription of its source occurrences"
                )
        elif observed:
            raise ValueError("Blank or unavailable table cell hides source occurrences")
