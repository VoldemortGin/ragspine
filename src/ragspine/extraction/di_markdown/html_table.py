"""DI markdown 的 HTML `<table>` → `TableGrid`（stdlib HTMLParser，容错）。

- `<th>` / `<td>` 都是格，`is_header` 区分；`<caption>` 单独保存；thead/tbody/tfoot 透明。
- rowspan / colspan 按 HTML 表格排布算法展开：被上方 rowspan 占住的位置向右跳过；
  rowspan 超出末行被裁剪；colspan 撞上已占位置被截短；非法跨度值回落为 1。
- 文本：实体解码（convert_charrefs）、行内标签丢弃、`<br>` 视作空格、空白折叠。
- 容错：未闭合的 td/th/tr/table 由下一个格/行或输入结束隐式闭合；嵌套表的文字并入外层格。
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser

from ragspine.extraction.di_markdown.models import TableCell, TableGrid

_MAX_COLSPAN = 1000  # HTML 规范上限
_MAX_ROWSPAN = 65534


@dataclass
class _RawCell:
    is_header: bool
    row_span: int
    col_span: int
    parts: list[str] = field(default_factory=list)
    start: tuple[int, int] = (1, 0)  # 起始标签的 HTMLParser.getpos()
    text_at: tuple[int, int] | None = None  # 首个非空文字的 getpos()


def _span(value: str | None, cap: int) -> int:
    try:
        n = int((value or "").strip())
    except ValueError:
        return 1
    return min(n, cap) if n >= 1 else 1


def _collapse(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_RawCell]] = []
        self.caption_parts: list[str] = []
        self._row: list[_RawCell] | None = None
        self._cell: _RawCell | None = None
        self._in_caption = False
        self._depth = 0
        self._done = False

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def _gap(self) -> None:
        if self._cell is not None:
            self._cell.parts.append(" ")
        elif self._in_caption:
            self.caption_parts.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "table":
            self._depth += 1
            if self._depth > 1:
                self._gap()
            return
        if self._depth > 1:
            if tag in ("tr", "td", "th", "br"):
                self._gap()
            return
        if tag == "caption":
            self._close_cell()
            self._in_caption = True
        elif tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            self._close_cell()
            self._in_caption = False
            if self._row is None:
                self._row = []
            attr = dict(attrs)
            self._cell = _RawCell(
                is_header=tag == "th",
                row_span=_span(attr.get("rowspan"), _MAX_ROWSPAN),
                col_span=_span(attr.get("colspan"), _MAX_COLSPAN),
                start=self.getpos(),
            )
        elif tag == "br":
            self._gap()

    def handle_endtag(self, tag: str) -> None:
        if self._done:
            return
        if tag == "table":
            if self._depth > 1:
                self._depth -= 1
                self._gap()
                return
            self._close_row()
            self._done = True
            return
        if self._depth > 1:
            return
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "caption":
            self._in_caption = False

    def handle_data(self, data: str) -> None:
        if self._done:
            return
        if self._cell is not None:
            if self._cell.text_at is None and data.strip():
                self._cell.text_at = self.getpos()
            self._cell.parts.append(data)
        elif self._in_caption:
            self.caption_parts.append(data)

    def finish(self) -> None:
        self.close()
        self._close_row()


def _layout(rows: list[list[_RawCell]]) -> TableGrid:
    return _grid(_place(rows), len(rows))


def _grid(placed: list[tuple[TableCell, _RawCell]], n_rows: int) -> TableGrid:
    n_cols = max((cell.col + cell.col_span for cell, _ in placed), default=0)
    cells = sorted((cell for cell, _ in placed), key=lambda cell: (cell.row, cell.col))
    return TableGrid(n_rows=n_rows, n_cols=n_cols, cells=tuple(cells))


def _place(rows: list[list[_RawCell]]) -> list[tuple[TableCell, _RawCell]]:
    n_rows = len(rows)
    occupied: set[tuple[int, int]] = set()
    cells: list[tuple[TableCell, _RawCell]] = []
    for r, row in enumerate(rows):
        c = 0
        for raw in row:
            while (r, c) in occupied:
                c += 1
            row_span = min(raw.row_span, n_rows - r)
            col_span = 1
            while col_span < raw.col_span and (r, c + col_span) not in occupied:
                col_span += 1
            for rr in range(r, r + row_span):
                for cc in range(c, c + col_span):
                    occupied.add((rr, cc))
            cell = TableCell(r, c, _collapse(raw.parts), raw.is_header, row_span, col_span)
            cells.append((cell, raw))
            c += col_span
    return cells


def parse_html_table(html: str) -> TableGrid:
    """解析一段 `<table>…</table>` HTML（首个外层表；其后内容忽略）→ TableGrid。永不抛异常。"""
    collector = _TableCollector()
    collector.feed(html)
    collector.finish()
    grid = _layout(collector.rows)
    caption = _collapse(collector.caption_parts) or None
    return TableGrid(grid.n_rows, grid.n_cols, grid.cells, caption)


def parse_html_table_with_offsets(html: str) -> tuple[TableGrid, dict[tuple[int, int], int]]:
    """同 `parse_html_table`，另给每个锚点格 `(row, col)` → 它在 `html` 中的偏移：首个非空文字处，
    没有文字时取起始标签处（跨页切表时据此判定格子属于哪一页）。"""
    collector = _TableCollector()
    collector.feed(html)
    collector.finish()
    placed = _place(collector.rows)
    line_starts = [0]
    newline = html.find("\n")
    while newline >= 0:
        line_starts.append(newline + 1)
        newline = html.find("\n", newline + 1)
    offsets = {}
    for cell, raw in placed:
        line, col = raw.text_at or raw.start
        offsets[(cell.row, cell.col)] = line_starts[line - 1] + col
    grid = _grid(placed, len(collector.rows))
    caption = _collapse(collector.caption_parts) or None
    return TableGrid(grid.n_rows, grid.n_cols, grid.cells, caption), offsets
