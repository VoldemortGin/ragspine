"""Verbatim row transcription of a table region whose grid was never proved (ADR 00NN).

A region the layout calls a table but whose rulings prove no grid (no frame, a frame
only, or a grid pdfspine cannot see) is still printed text. Its occurrences are grouped
into the lines they are printed on — by geometry alone — and each line is read left to
right. Nothing here infers a column, a header, a merge or a value: a *row* is only "these
spans share a printed line", the texts are the spans' own characters, and the cells of a
row are joined by ``ROW_SEPARATOR`` so the row is recoverable span by span. Producer and
validator share ``table_rows`` so a row admitted at processing time re-derives at index
and resolve time.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.page.geometry import contains

# Distinct from ``exact-source-transcription-v1`` (a verified cell grid's transcription) and
# from the grid scope: a row member says "these spans share a printed line", nothing more.
TABLE_ROWS_PRODUCER = "table-rows-verbatim-v1"
TABLE_ROWS_SCOPE = "verbatim-table-rows-v1"
TABLE_ROWS_METHOD = (
    "deterministic printed-line transcription of source occurrences; "
    "table structure (columns, headers, merges) is not verified"
)
# Between the cells of one row. A tab is whitespace to both retrieval channels and to the
# claim re-read (which folds whitespace); the IR keeps each span apart anyway.
ROW_SEPARATOR = "\t"
# Two spans share a printed line when their vertical overlap is at least this share of the
# shorter one: scale-free, so it holds for any font size, a jittered baseline and a raised
# footnote marker, while two lines set solid never overlap that much.
ROW_OVERLAP = 0.5


@dataclass(frozen=True, slots=True)
class TableRow:
    """One printed line of the region: its spans left to right, their texts verbatim."""

    row_id: str
    bbox: Bounds
    source_span_ids: tuple[str, ...]
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.source_span_ids or len(self.source_span_ids) != len(self.texts):
            raise ValueError("A table row pairs each source occurrence with its text")

    @property
    def text(self) -> str:
        return ROW_SEPARATOR.join(self.texts)


@dataclass(frozen=True, slots=True)
class TableRowsIR:
    """A table region transcribed as printed rows; its grid is not claimed at all."""

    object_id: str
    source: SourceAnchor
    rows: tuple[TableRow, ...]
    producer: str = TABLE_ROWS_PRODUCER

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError("A row transcription holds at least one row")
        if tuple(row.row_id for row in self.rows) != tuple(
            f"row-{index}" for index in range(len(self.rows))
        ):
            raise ValueError("Row ids are dense and ordered")
        if len(set(self.source_span_ids)) != len(self.source_span_ids):
            raise ValueError("A source occurrence belongs to one row only")

    @property
    def source_span_ids(self) -> tuple[str, ...]:
        """Every occurrence in row order; also the description's span order."""
        return tuple(span_id for row in self.rows for span_id in row.source_span_ids)


def _height(span: TextSpan) -> float:
    return span.bbox[3] - span.bbox[1]


def _same_line(first: TextSpan, second: TextSpan) -> bool:
    shorter = min(_height(first), _height(second))
    overlap = min(first.bbox[3], second.bbox[3]) - max(first.bbox[1], second.bbox[1])
    if shorter <= 0:
        return overlap >= 0
    return overlap >= ROW_OVERLAP * shorter


def group_rows(spans: Sequence[TextSpan]) -> tuple[tuple[TextSpan, ...], ...]:
    """Group ``spans`` (page order) into printed lines, top down, each read left to right.

    Spans are visited by vertical centre; one joins the current line when it shares a line
    with that line's tallest span so far (a small raised marker never decides the line),
    else it opens the next. Ties keep page order, so the result depends on the page alone.
    """
    ordered = sorted(
        enumerate(spans),
        key=lambda item: ((item[1].bbox[1] + item[1].bbox[3]) / 2, item[1].bbox[0], item[0]),
    )
    lines: list[list[tuple[int, TextSpan]]] = []
    tallest: list[TextSpan] = []
    for index, span in ordered:
        if lines and _same_line(span, tallest[-1]):
            lines[-1].append((index, span))
            if _height(span) > _height(tallest[-1]):
                tallest[-1] = span
        else:
            lines.append([(index, span)])
            tallest.append(span)
    return tuple(
        tuple(span for _, span in sorted(line, key=lambda item: (item[1].bbox[0], item[0])))
        for line in lines
    )


def table_rows(object_id: str, source: SourceAnchor, spans: Sequence[TextSpan]) -> TableRowsIR:
    """The region's rows from its own occurrences (page order); refuses an empty region."""
    if not spans:
        raise ValueError("No source text is available for this table region")
    rows = tuple(
        TableRow(
            f"row-{index}",
            (
                min(span.bbox[0] for span in line),
                min(span.bbox[1] for span in line),
                max(span.bbox[2] for span in line),
                max(span.bbox[3] for span in line),
            ),
            tuple(span.span_id for span in line),
            tuple(span.text for span in line),
        )
        for index, line in enumerate(group_rows(spans))
    )
    return TableRowsIR(object_id, source, rows)


def rows_text(ir: TableRowsIR) -> str:
    """The member's description and index text: one row per line, cells tab-separated."""
    return "\n".join(row.text for row in ir.rows)


def check_table_rows(ir: TableRowsIR, page_spans: Sequence[TextSpan], *, anchor: Bounds) -> None:
    """Raise ``ValueError`` unless ``ir`` is exactly what its occurrences re-derive to.

    ``page_spans`` is the page's text layer in page order; the occurrences the IR names are
    taken from it, each must lie inside ``anchor``, and grouping them again must give the
    stored rows character for character.
    """
    named = set(ir.source_span_ids)
    owned = tuple(span for span in page_spans if span.span_id in named)
    if len(owned) != len(named):
        raise ValueError("Table row references an unknown source occurrence")
    if any(not contains(anchor, span.bbox) for span in owned):
        raise ValueError("Table row source occurrence is outside its qualified anchor")
    if table_rows(ir.object_id, ir.source, owned) != ir:
        raise ValueError("Table rows do not re-derive from their source occurrences")
