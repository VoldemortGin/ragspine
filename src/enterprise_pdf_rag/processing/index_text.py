"""Deterministic index text per member: the one string both retrieval channels score.

A chart's embedded description is usually its title alone (``Distribution Mix``), so
a question that names the chart's categories or values but not its title has nothing
to match. ``chart_index_text`` projects the qualified IR instead: title, period,
grammar and every point's category / series / explicit value. A chart without a
single explicit value keeps its description text, so a pending or label-only figure
never climbs the ranking on words it cannot cite. A proven diagram is projected the same
way: its node labels in reading order plus one ``<from> -> <to>`` pair per drawn edge.
Nothing here reads a store.

Two index-layout switches (``IndexTextOptions``) change *how many* units a member scores
as, never what a unit says: a long verbatim-rows table (ADR 0027) or pending inferred grid
(ADR 0031) becomes one unit per figure row with its header rows repeated, and a running header / footer scores as no unit
at all. Every character of a unit is still printed by the table or by the page context.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from ragspine.extraction.evidence.figures.models import ChartIR, ChartPoint, ValueKind
from ragspine.extraction.evidence.objects.diagrams.diagram_description import (
    EDGE_ARROW,
    reading_order,
)
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    inferred_header_rows,
    structure_producer,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.objects.tables.table_rows import TableRow, TableRowsIR
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, FormulaIR, TypedIR

# The index layout every snapshot had before the switches: one unit, one vector per member.
INDEX_VERSION = "immutable-cosine-index-v1"
# A unit index may hold several vectors per member (its units) or none (an unscored member).
UNIT_INDEX_VERSION = "immutable-cosine-unit-index-v1"
_ROW_UNITS = "table-row-units-v1"
_RUNNING = "running-lines-unscored-v1"
# Header rows are the rows above the first figure row; deeper than this is not a header we
# can trust, so only the first row is repeated.
MAX_HEADER_ROWS = 4
# A table never scores as more units than this; a longer one puts consecutive figure rows
# together. 64 keeps a 200-row note at about 3 rows a unit and bounds its vectors.
MAX_UNITS_PER_TABLE = 64
# A printed figure: digits with separators, an accounting negative, a sign, a currency
# prefix or a percent. A bare four-digit year (``2024``) heads a column, it is not a figure.
_FIGURE = re.compile(r"[(\-−–]?[$€£¥]?\d[\d,.]*%?\)?")  # noqa: RUF001
_YEAR = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True, slots=True)
class IndexTextOptions:
    """How a snapshot's index text is laid out into scoring units; both off = one unit each."""

    # A verbatim-rows table scores as one unit per figure row, its header rows repeated.
    table_row_units: bool = False
    # A Text member every line of which is a running header / footer scores as no unit.
    drop_running_lines: bool = False

    @property
    def index_version(self) -> str:
        features = [
            name
            for name, on in (
                (_ROW_UNITS, self.table_row_units),
                (_RUNNING, self.drop_running_lines),
            )
            if on
        ]
        return f"{UNIT_INDEX_VERSION}:{'+'.join(features)}" if features else INDEX_VERSION

    @classmethod
    def from_index_version(cls, version: str) -> "IndexTextOptions":
        """The switches a snapshot was indexed with; any non-unit version had none."""
        if not version.startswith(UNIT_INDEX_VERSION + ":"):
            return cls()
        features = set(version.removeprefix(UNIT_INDEX_VERSION + ":").split("+"))
        if not features or not features <= {_ROW_UNITS, _RUNNING}:
            raise ValueError(f"unknown index version {version!r}")
        return cls(_ROW_UNITS in features, _RUNNING in features)


# Both switches off: the layout every snapshot had before them.
ONE_UNIT_EACH = IndexTextOptions()


@dataclass(frozen=True, slots=True)
class PageIndexContext:
    """The page context prepended to a member's index text (policy v4): none is optional."""

    display_title: str | None = None
    page_title: str | None = None
    section: str | None = None

    def header(self) -> str:
        parts = (
            " ".join(part.split())
            for part in (self.display_title, self.page_title, self.section)
            if part is not None
        )
        return " | ".join(part for part in parts if part)


def contextual_index_text(body: str, context: PageIndexContext | None) -> str:
    """``<display_title> | <page_title> | <section>`` on its own line above ``body``.

    Only the text both retrieval channels score changes; descriptions and quoted
    evidence are untouched. Without any context the body is returned as is.
    """
    header = "" if context is None else context.header()
    return f"{header}\n{body}" if header else body


def _explicit(point: ChartPoint) -> bool:
    return point.value.kind is ValueKind.EXPLICIT and point.value.value is not None


def has_citable_value(chart: ChartIR) -> bool:
    """True when at least one point carries an explicit numeric value an answer may cite."""
    return any(_explicit(point) for point in chart.points)


def _value_text(value: Decimal, unit: str) -> str:
    unit = unit.strip()
    if unit and not any(char.isalnum() for char in unit):
        return f"{value}{unit}"  # ``72%``
    return f"{value} {unit}".strip()  # ``15 US cents``


def chart_index_text(chart: ChartIR, *, fallback: str) -> str:
    """Project a chart with explicit values into searchable text; otherwise ``fallback``.

    The projection is deterministic: ``<title> <period> <grammar> chart figure`` followed
    by ``<category> <series> <value><unit>`` per point (labels only for points whose
    value is unavailable). ``fallback`` is the chart's description text, which is what
    charts embedded before this projection existed.
    """
    if not has_citable_value(chart):
        return fallback
    parts: list[str] = []
    if chart.title is not None:
        parts.append(chart.title.text)
    if chart.period is not None:
        parts.append(chart.period.text)
    parts.append(f"{chart.grammar} chart figure")
    for point in chart.points:
        parts.extend((point.category.text, point.series.text))
        if _explicit(point):
            assert point.value.value is not None
            parts.append(_value_text(point.value.value, point.unit.text))
    return " ".join(part.strip() for part in parts if part.strip())


def has_citable_structure(diagram: DiagramIR) -> bool:
    """True when at least one node label is verbatim source text an answer may cite."""
    return any(node.label.strip() and node.source_span_ids for node in diagram.nodes)


def diagram_index_text(diagram: DiagramIR, *, fallback: str) -> str:
    """Project a proven diagram into searchable text; otherwise ``fallback``.

    The projection is deterministic: ``diagram figure`` followed by every node label in
    reading order and then ``<from> -> <to>`` per proven edge, the same pair a
    ``diagram_edge`` claim cites. ``fallback`` is the member's description text.
    """
    if not has_citable_structure(diagram):
        return fallback
    by_id = {node.node_id: node.label for node in diagram.nodes}
    parts = ["diagram figure", *(node.label for node in reading_order(diagram.nodes))]
    parts.extend(
        f"{by_id[edge.source_node_id]}{EDGE_ARROW}{by_id[edge.target_node_id]}"
        for edge in diagram.edges
    )
    return " ".join(part.strip() for part in parts if part.strip())


def formula_index_text(formula: FormulaIR, *, fallback: str) -> str:
    """Project a proven formula into searchable text; otherwise ``fallback``.

    The projection is deterministic: the readable transcription, its linear form, the
    literal word ``formula`` and every distinct token text, so a question that names a
    symbol or an operand rather than the surrounding prose has something to match. A
    model-only ``FormulaIR`` carries no token, so it keeps its description text.
    """
    if not formula.tokens or formula.linear is None or formula.readable is None:
        return fallback
    parts = [formula.readable, formula.linear, "formula"]
    parts.extend(dict.fromkeys(token.text for token in formula.tokens))
    return " ".join(part.strip() for part in parts if part.strip())


def _figure_row(row: TableRow) -> bool:
    """A row printing at least one figure cell that is not a bare year."""
    return any(
        _FIGURE.fullmatch(text.strip()) and not _YEAR.fullmatch(text.strip()) for text in row.texts
    )


def table_header_rows(ir: TableRowsIR) -> int:
    """How many leading rows every unit repeats: those above the first figure row.

    Conservative: a table opening on a figure row, or whose header would be deeper than
    ``MAX_HEADER_ROWS``, repeats its first row only.
    """
    first = next((index for index, row in enumerate(ir.rows) if _figure_row(row)), None)
    if first is None or first == 0 or first > MAX_HEADER_ROWS:
        return 1
    return first


def table_row_units(ir: TableRowsIR, context: PageIndexContext | None) -> tuple[str, ...] | None:
    """The scoring units of a verbatim-rows table, or ``None`` when it stays one unit.

    Each unit is the page context header, the table's header rows and one figure row with
    the label-only rows printed just above it (a wrapped label, a sub-heading); rows after
    the last figure row join the last unit. Rows are the IR's own row text, so no character
    is added. A table with fewer than two figure rows below its header is not split, and a
    table longer than ``MAX_UNITS_PER_TABLE`` units puts consecutive groups together.
    """
    rows = tuple((row.text, _figure_row(row)) for row in ir.rows)
    return _row_units(rows, table_header_rows(ir), context)


def inferred_table_row_units(
    ir: TableIR, context: PageIndexContext | None
) -> tuple[str, ...] | None:
    """The scoring units of an ADR 0031 pending grid, laid out like ``table_row_units``.

    A row's text is its cells' own text left to right, tab separated (a spanning cell once,
    a blank slot not at all); the repeated header is the grid's inferred header rows, capped
    like a verbatim-rows header. ``None`` for any other grid: a proved grid keeps one unit.
    """
    if structure_producer(ir) is None:
        return None
    rows: list[tuple[str, bool]] = []
    for row in range(ir.row_count):
        cells = sorted((cell for cell in ir.cells if cell.row == row), key=lambda cell: cell.col)
        texts = [cell.text for cell in cells if cell.text]
        figure = any(
            cell.col >= 1
            and cell.text is not None
            and _FIGURE.fullmatch(cell.text.strip()) is not None
            and _YEAR.fullmatch(cell.text.strip()) is None
            for cell in cells
        )
        rows.append(("\t".join(texts), figure))
    depth = inferred_header_rows(ir)
    if depth == 0 or depth > MAX_HEADER_ROWS:
        depth = 1
    return _row_units(tuple(rows), depth, context)


def _row_units(
    rows: tuple[tuple[str, bool], ...], depth: int, context: PageIndexContext | None
) -> tuple[str, ...] | None:
    head = tuple(text for text, _ in rows[:depth])
    groups: list[list[str]] = []
    pending: list[str] = []
    for text, figure in rows[depth:]:
        pending.append(text)
        if figure:
            groups.append(pending)
            pending = []
    if len(groups) < 2:
        return None
    groups[-1].extend(pending)
    size = ceil(len(groups) / MAX_UNITS_PER_TABLE)
    return tuple(
        contextual_index_text(
            "\n".join((*head, *(text for group in groups[start : start + size] for text in group))),
            context,
        )
        for start in range(0, len(groups), size)
    )


def member_index_text(ir: TypedIR, description_text: str) -> str:
    """Text / list / group / table members index their description; figures are projected."""
    if isinstance(ir, ChartIR):
        return chart_index_text(ir, fallback=description_text)
    if isinstance(ir, DiagramIR):
        return diagram_index_text(ir, fallback=description_text)
    if isinstance(ir, FormulaIR):
        return formula_index_text(ir, fallback=description_text)
    return description_text
