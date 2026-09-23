"""DI markdown 中间表示：文档 → 页 → 块（Heading / Paragraph / Table / Figure）。

全部为 frozen dataclass，纯数据、零依赖。表格坐标 0 起；页号 1 起。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableCell:
    """一个锚点格：位于展开网格的 (row, col)，跨 row_span 行 × col_span 列。

    被合并吞掉的位置不产出独立 TableCell；用 `TableGrid.anchor_at` 反查覆盖它的锚点。
    """

    row: int
    col: int
    text: str
    is_header: bool
    row_span: int = 1
    col_span: int = 1

    @property
    def is_merged(self) -> bool:
        return self.row_span > 1 or self.col_span > 1


@dataclass(frozen=True)
class TableGrid:
    """HTML 表展开后的矩形网格；`cells` 为行优先的锚点格（含合并跨度）。"""

    n_rows: int
    n_cols: int
    cells: tuple[TableCell, ...]
    caption: str | None = None

    def anchor_at(self, row: int, col: int) -> TableCell | None:
        """覆盖 (row, col) 的锚点格；空洞（参差行补齐处）返回 None。"""
        for cell in self.cells:
            if cell.row <= row < cell.row + cell.row_span and (
                cell.col <= col < cell.col + cell.col_span
            ):
                return cell
        return None

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        """矩形文本网格：合并覆盖的每个位置都重复锚点文本，空洞为 ""。"""
        grid = [[""] * self.n_cols for _ in range(self.n_rows)]
        for cell in self.cells:
            for r in range(cell.row, cell.row + cell.row_span):
                for c in range(cell.col, cell.col + cell.col_span):
                    grid[r][c] = cell.text
        return tuple(tuple(row) for row in grid)

    @property
    def header_row_count(self) -> int:
        """自顶向下连续「每个位置都被 <th> 锚点覆盖」的行数。"""
        header_slots: set[tuple[int, int]] = set()
        for cell in self.cells:
            if cell.is_header:
                for r in range(cell.row, cell.row + cell.row_span):
                    for c in range(cell.col, cell.col + cell.col_span):
                        header_slots.add((r, c))
        count = 0
        for r in range(self.n_rows):
            if not all((r, c) in header_slots for c in range(self.n_cols)):
                break
            count += 1
        return count


@dataclass(frozen=True)
class Heading:
    """`#`–`######` 标题；heading_path 含自身（标题栈在它入栈之后的快照）。"""

    text: str
    level: int
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class Paragraph:
    """空行分隔的段落；段内换行保留为 "\\n"。"""

    text: str
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    grid: TableGrid
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class Figure:
    """`<figure>` 容器：caption 取自 `<figcaption>`，text 为其余非空行（"\\n" 连接）。"""

    text: str
    caption: str | None
    heading_path: tuple[str, ...]


Block = Heading | Paragraph | Table | Figure


@dataclass(frozen=True)
class DiPage:
    """一页。index = 物理页序（1 起）；number = PageNumber 注释可解析时取之，否则 = index。"""

    index: int
    number: int
    page_number_label: str | None
    headers: tuple[str, ...]
    footers: tuple[str, ...]
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class DiDocument:
    pages: tuple[DiPage, ...]
