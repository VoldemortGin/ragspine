"""Azure Document Intelligence 风格 markdown → `DiDocument`（页 → 块）。

格式依据：Microsoft Learn「Document Intelligence supported Markdown elements」
（prebuilt-layout, `outputContentFormat=markdown`）。约定与本解析器的取法：

- **分页**：`<!-- PageBreak -->` 为页间分隔符，页数 = 分隔符数 + 1（空输入 = 1 个空页）。
  `DiPage.index` 为物理页序（1 起）。
- **页标记（marker 模式，ADR 0027）**：全文只要有一个整体为 `page: N` 的注释（`<!-- page: N -->`，
  忽略大小写与空白；SuperIndex azure_di 抽取器在每页起点插入，N 为原 PDF 真实页码）且 N 不超过
  `MAX_MARKER_PAGE`（10000，超限的不算页标记、按普通注释删除）就改由它分页，`has_page_markers` 判定；
  此时 PageBreak 不再分页，按未知注释删除。第 N 页放在第 N 个位置（`DiPage.index == N`），没有标记的页
  补空页（`blocks=()`，`number=index`），页数 = 最大 N。`page: 0` 按 1；同一 N 重复或倒序时按 N 归桶、
  桶内按文档顺序拼接。第一个标记之前的内容归入第一个标记所在的页；行内标记在标记处切开。
- **被页标记切开的表**（起始标签在行首、有配对 `</table>`）：整表解析一次，每个格按它首个文字（无文字取
  起始标签）所在的页归页，改写成每页一张表：表头块（`header_row_count` 行）每页都有，跨出表头块的
  rowspan 在复制时截断到块内；本页的数据行照原位置输出；从前一页跨进来的 rowspan 锚点在本页首行重出
  （行数取剩余）；被切开的行把行标签格（该行的 `<th>`，没有 th 时取第 0 列且不是纯数值）复制到后续各页的
  同一位置，其余属于别页的格留空占位，保证列对齐。数值格不重复、不丢失，跨页的 rowspan 锚点和被切开行的
  行标签在两页各出现一次。表内其他注释（PageHeader 等）留在所在页。没有配对 `</table>` 的表不改写，仍按
  「未闭合的表延伸到本页末」处理；落在 `<figure>` 内不修复。
- **页码**：`<!-- PageNumber="..." -->` 的标签恰含一个整数（如 "12"、"Page 12"、"- 12 -"）
  时取之作 `DiPage.number`；无注释或不可解析（"iv"、"3 of 10"）时回落为物理页序。
  多条时取第一条可解析的；原标签保存在 `page_number_label`。
- **页元数据不进正文**：PageHeader / PageFooter 值分别收进 `headers` / `footers`；
  这三类与其他任何完整的 `<!-- ... -->` 注释都从正文删除。独占一行的注释连同该行删除
  （不会把段落切断）；未闭合的 `<!--` 按普通文本保留。
- **标题**：ATX `#`–`######`（`#` 后需空白；可选闭合 `#` 序列去掉）。标题栈跨页延续：
  level n 入栈前弹出所有 level ≥ n 的项；每个块的 `heading_path` 是当时栈的快照。
- **段落**：空行分段；段内各行 strip 后以 "\\n" 连接；标题行 / `<table` / `<figure` 行也会结束段落。
- **表格**：以 `<table` 开头的行起，到配对的 `</table>`（计嵌套深度）为止，交给
  `html_table.parse_html_table`；未闭合则延伸到本页末。
- **图**：`<figure` 起到 `</figure>`（未闭合则到本页末）为一个块；`<figcaption>` → caption。
- **文本**：段落 / 标题 / 图 / 页元数据都做 HTML 实体解码（DI 替身会转义 `<`、`>`、`&`）。

纯函数、只用 stdlib、永不因畸形输入抛异常。
"""

import html
import re
import unicodedata
from bisect import bisect_right

from ragspine.extraction.di_markdown.html_table import (
    parse_html_table,
    parse_html_table_with_offsets,
)
from ragspine.extraction.di_markdown.models import (
    Block,
    DiDocument,
    DiPage,
    Figure,
    Heading,
    Paragraph,
    Table,
    TableCell,
    TableGrid,
)

_PAGE_BREAK = re.compile(r"<!--\s*PageBreak\s*-->")
_PAGE_MARKER = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)
MAX_MARKER_PAGE = 10_000  # 页号上限：缺页要补空页，超限的标记不算页标记（防一行注释撑出上亿空页）
_LINE_COMMENT = re.compile(
    r"^[ \t]*<!--((?:(?!-->).)*)-->[ \t]*(?:\n|\Z)", re.MULTILINE | re.DOTALL
)
_INLINE_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_META = re.compile(r'^\s*(PageHeader|PageFooter|PageNumber)\s*=\s*"(.*)"\s*$', re.DOTALL)
_SINGLE_INT = re.compile(r"^\D*(\d+)\D*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+$")
_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)
_TABLE_TAG = re.compile(r"<(/?)table\b[^>]*>?", re.IGNORECASE)
_FIGURE_OPEN = re.compile(r"<figure\b[^>]*>", re.IGNORECASE)
_FIGURE_CLOSE = re.compile(r"</figure\s*>", re.IGNORECASE)
_CURRENCY = re.compile(
    r"^(?:US\$|HK\$|NT\$|S\$|A\$|C\$|RMB|CNY|USD|HKD|EUR|GBP|JPY|\$|¥|€|£|₩|₹)", re.IGNORECASE
)
_UNIT = re.compile(
    r"(?:bn|mn|tn|bps|bp|pp|usd|hkd|rmb|cny|eur|gbp|jpy|%|x|k|m|b|t)$", re.IGNORECASE
)
_PLAIN_NUMBER = re.compile(r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)(?:\.\d+)?|\.\d+")
_MINUS = str.maketrans({"\u2212": "-", "\u2012": "-", "\u2013": "-", "\ufe63": "-"})
_FIGCAPTION = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption\s*>", re.IGNORECASE | re.DOTALL)


class _Meta:
    def __init__(self) -> None:
        self.headers: list[str] = []
        self.footers: list[str] = []
        self.page_numbers: list[str] = []

    def take(self, match: re.Match[str]) -> str:
        meta = _META.match(match.group(1))
        if meta is not None:
            kind, value = meta.group(1), html.unescape(meta.group(2).strip())
            if kind == "PageHeader":
                self.headers.append(value)
            elif kind == "PageFooter":
                self.footers.append(value)
            else:
                self.page_numbers.append(value)
        return ""


def _page_number(labels: list[str], index: int) -> tuple[int, str | None]:
    for label in labels:
        m = _SINGLE_INT.match(label)
        if m is not None:
            return int(m.group(1)), label
    return index, (labels[0] if labels else None)


def _table_end(body: str, start: int) -> int:
    depth = 0
    for m in _TABLE_TAG.finditer(body, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.end()
    return len(body)


def _figure(inner: str, path: tuple[str, ...]) -> Figure:
    captions = [" ".join(html.unescape(m).split()) for m in _FIGCAPTION.findall(inner)]
    rest = _FIGCAPTION.sub("", inner)
    lines = [html.unescape(line.strip()) for line in rest.split("\n") if line.strip()]
    return Figure(
        text="\n".join(lines),
        caption=" ".join(c for c in captions if c) or None,
        heading_path=path,
    )


def _path(stack: list[tuple[int, str]]) -> tuple[str, ...]:
    return tuple(text for _, text in stack)


def _flush(blocks: list[Block], para: list[str], stack: list[tuple[int, str]]) -> None:
    # 模块级而非 _blocks 内的闭包：beartype claw 会在每次调用时重新包装内层函数，逐页都编译一遍
    if para:
        blocks.append(Paragraph(text="\n".join(para), heading_path=_path(stack)))
        para.clear()


def _blocks(body: str, stack: list[tuple[int, str]], tables: dict[str, TableGrid]) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    pos, n = 0, len(body)
    while pos < n:
        eol = body.find("\n", pos)
        eol = n if eol < 0 else eol
        line = body[pos:eol]
        stripped = line.strip()
        lead = pos + len(line) - len(line.lstrip())
        heading = _HEADING.match(line)
        if not stripped:
            _flush(blocks, para, stack)
            pos = eol + 1
        elif heading is not None:
            _flush(blocks, para, stack)
            level = len(heading.group(1))
            text = html.unescape(_CLOSING_HASHES.sub("", heading.group(2) or "").strip())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            blocks.append(Heading(text=text, level=level, heading_path=_path(stack)))
            pos = eol + 1
        elif _TABLE_OPEN.match(stripped):
            _flush(blocks, para, stack)
            end = _table_end(body, lead)
            source = body[lead:end]
            grid = tables.get(source)  # 拆表时已直接建好的网格，免得逐页再解析一遍
            grid = parse_html_table(source) if grid is None else grid
            blocks.append(Table(grid=grid, heading_path=_path(stack)))
            pos = end
        elif (opener := _FIGURE_OPEN.match(body, lead)) is not None:
            _flush(blocks, para, stack)
            close = _FIGURE_CLOSE.search(body, opener.end())
            inner_end, pos = (close.start(), close.end()) if close else (n, n)
            blocks.append(_figure(body[opener.end() : inner_end], _path(stack)))
        else:
            para.append(html.unescape(stripped))
            pos = eol + 1
    _flush(blocks, para, stack)
    return blocks


def _page_markers(text: str) -> list[tuple[re.Match[str], int]]:
    marks: list[tuple[re.Match[str], int]] = []
    for m in _PAGE_MARKER.finditer(text):
        digits = m.group(1).lstrip("0") or "0"
        if len(digits) <= len(str(MAX_MARKER_PAGE)) and int(digits) <= MAX_MARKER_PAGE:
            marks.append((m, max(1, int(digits))))
    return marks


def has_page_markers(text: str) -> bool:
    """全文是否含页号不超过 `MAX_MARKER_PAGE` 的 `<!-- page: N -->` 页标记（即 marker 模式）。"""
    return bool(_page_markers(text))


def page_marker_numbers(text: str) -> frozenset[int]:
    """全文出现过的合法页标记页号（去重，`page: 0` 按 1，超限的不算）；无标记为空集。"""
    return frozenset(page for _, page in _page_markers(text))


def _closed_tables(text: str) -> list[tuple[int, int]]:
    """最外层的已闭合 `<table>…</table>` 区间（起始标签独占行首）。每张表按栈独立配对：
    没有配对 `</table>` 的表整张放弃，不影响它后面的表。"""
    closed: list[tuple[int, int]] = []
    opened: list[int] = []
    for tag in _TABLE_TAG.finditer(text):
        if not tag.group(1):
            opened.append(tag.start())
        elif opened:  # 游离的 </table> 忽略
            closed.append((opened.pop(), tag.end()))
    spans: list[tuple[int, int]] = []
    outer_end = -1
    for start, end in sorted(closed):
        if start < outer_end:
            continue  # 嵌在已取的外层表里
        outer_end = end
        if text[text.rfind("\n", 0, start) + 1 : start].strip() == "":
            spans.append((start, end))
    return spans


def _cell_html(cell: TableCell, text: str, row_span: int, *, placeholder: bool = False) -> str:
    tag = "th" if cell.is_header and not placeholder else "td"
    attrs = f' rowspan="{row_span}"' if row_span > 1 else ""
    attrs += f' colspan="{cell.col_span}"' if cell.col_span > 1 else ""
    return f"<{tag}{attrs}>{html.escape(text, quote=False)}</{tag}>"


def _page_table(
    grid: TableGrid,
    owner: dict[tuple[int, int], TableCell],
    page_of: dict[TableCell, int],
    page: int,
    head: int,
    rows: list[int],
    labels: set[TableCell],
    caption: bool,
) -> tuple[str, TableGrid]:
    """跨页表在 `page` 上的那部分：表头块整块复制（跨出表头块的 rowspan 截断在块内），
    再接本页的数据行；从前面的页跨进来的 rowspan 锚点在本页首行重出（剩余行数）；被切开的行
    把前页的行标签格（`labels`）复制过来，同一行里属于别页的其他格留空占位；列位置不变。"""
    claimed: set[tuple[int, int]] = set()
    placed: list[TableCell] = []
    lines = ["<table>"]
    if caption and grid.caption:
        lines.append(f"<caption>{html.escape(grid.caption, quote=False)}</caption>")
    for i, r in enumerate(rows):
        cells: list[str] = []
        for c in range(grid.n_cols):
            if (i, c) in claimed:
                continue
            anchor = owner.get((r, c))
            if anchor is None:
                cells.append("<td></td>")
                placed.append(TableCell(i, c, "", False))
                continue
            native = page_of[anchor] == page
            limit = anchor.row + anchor.row_span
            if r < head and not native:
                limit = min(limit, head)
            text, placeholder = anchor.text, False
            if r >= head and anchor.row == r and not native:
                if anchor not in labels or page < page_of[anchor]:
                    # 占位只占本行（下一行起按跨页 rowspan 重出），一律 td，不改变表头判定
                    text, limit, placeholder = "", r + 1, True
            span = 1
            while i + span < len(rows) and rows[i + span] < limit:
                span += 1
            claimed.update(
                (ii, cc)
                for ii in range(i, i + span)
                for cc in range(anchor.col, anchor.col + anchor.col_span)
            )
            cells.append(_cell_html(anchor, text, span, placeholder=placeholder))
            is_header = anchor.is_header and not placeholder
            placed.append(TableCell(i, anchor.col, text, is_header, span, anchor.col_span))
        lines.append("<tr>" + "".join(cells) + "</tr>")
    lines.append("</table>")
    n_cols = max((cell.col + cell.col_span for cell in placed), default=0)
    page_grid = TableGrid(
        len(rows), n_cols, tuple(placed), grid.caption if caption and grid.caption else None
    )
    return "\n".join(lines), page_grid


def _is_value_like(text: str) -> bool:
    """单元格文字看起来是不是一个数值（行标签过滤用）：NFKC 归一化（全角、上标、不换行空格），
    去脚注星号，再逐层剥掉正负号 / Unicode 减号、括号负数、货币前缀、单位后缀，剩下的须是一个
    数（千分位逗号或空格、小数）。年份也算数值；空串和纯标点不算。"""
    value = unicodedata.normalize("NFKC", text).translate(_MINUS).strip().rstrip("*†‡").strip()
    while True:
        before = value
        if value[:1] in ("+", "-"):
            value = value[1:].strip()
        if value.startswith("(") and value.endswith(")"):
            value = value[1:-1].strip()
        value = _UNIT.sub("", _CURRENCY.sub("", value).strip()).strip()
        if value == before:
            return _PLAIN_NUMBER.fullmatch(value) is not None


def _row_labels(
    grid: TableGrid, owner: dict[tuple[int, int], TableCell], row: int
) -> list[TableCell]:
    """行标签格：该行行首连续的非数值 `<th>` 格；没有时取第 0 列（非空、不像数值）。"""
    labels: list[TableCell] = []
    col = 0
    while (cell := owner.get((row, col))) is not None and cell.row == row and cell.is_header:
        if not cell.text.strip() or _is_value_like(cell.text):
            break
        labels.append(cell)
        col = cell.col + cell.col_span
    if labels:
        return labels
    first = owner.get((row, 0))
    if first is None or first.row != row or not first.text.strip() or _is_value_like(first.text):
        return []
    return [first]


def _split_table(
    source: str, bounds: list[int], pages: list[int], first: int
) -> dict[int, tuple[str, TableGrid]]:
    """把一张被页标记切开的表按格所在的页拆成每页一张表：{页号: (表 HTML, 它的网格)}。
    复杂度 O(格子数 + 各页输出)：页 → 行的索引只建一次。"""
    grid, offsets = parse_html_table_with_offsets(source)

    def page_at(offset: int) -> int:
        k = bisect_right(bounds, offset)
        return first if k == 0 else pages[k - 1]

    owner: dict[tuple[int, int], TableCell] = {}
    page_of: dict[TableCell, int] = {}
    starts: dict[int, list[int]] = {}
    for cell in grid.cells:
        page_of[cell] = page_at(offsets[(cell.row, cell.col)])
        starts.setdefault(cell.row, []).append(page_of[cell])
        for r in range(cell.row, cell.row + cell.row_span):
            for c in range(cell.col, cell.col + cell.col_span):
                owner[(r, c)] = cell
    head = grid.header_row_count
    row_pages = [set(starts.get(r, ())) for r in range(head, grid.n_rows)]  # 每行至少起一个格
    page_rows: dict[int, list[int]] = {first: []}
    for k, ps in enumerate(row_pages):
        for page in ps:
            page_rows.setdefault(page, []).append(head + k)
    labels = {
        label
        for k, ps in enumerate(row_pages)
        if len(ps) > 1
        for label in _row_labels(grid, owner, head + k)
    }
    return {
        page: _page_table(
            grid,
            owner,
            page_of,
            page,
            head,
            list(range(head)) + rows,
            labels,
            caption=page == first,
        )
        for page, rows in sorted(page_rows.items())
    }


def _split_tables(
    text: str, marks: list[tuple[re.Match[str], int]], grids: dict[str, TableGrid]
) -> str:
    """被页标记切开、且有配对 `</table>` 的表，改写为每页一张完整的表（标记原位保留，
    表内其他注释如 PageHeader / PageNumber 跟在该页的表后面）；每张新表的网格记入 `grids`。"""
    out: list[str] = []
    pos = 0
    starts = [m.start() for m, _ in marks]
    for s, e in _closed_tables(text):
        lo = bisect_right(starts, s)
        inside = marks[lo : bisect_right(starts, e)]
        if not inside:
            continue
        first = marks[lo - 1][1] if lo > 0 else marks[0][1]
        tables = _split_table(
            text[s:e], [m.start() - s for m, _ in inside], [p for _, p in inside], first
        )
        cuts = [s] + [m.end() for m, _ in inside]
        ends = [m.start() for m, _ in inside] + [e]
        piece_pages = [first] + [p for _, p in inside]
        out.append(text[pos:s])
        for j, (a, b, page) in enumerate(zip(cuts, ends, piece_pages, strict=True)):
            comments = "".join(f"\n{c.group(0)}" for c in _INLINE_COMMENT.finditer(text, a, b))
            table_html = ""
            if page in tables:
                table_html, grids[table_html] = tables.pop(page)
            out.append(f"\n{table_html}{comments}\n")
            if j < len(inside):
                out.append(inside[j][0].group(0))
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _marker_pages(text: str, grids: dict[str, TableGrid]) -> list[tuple[int, str]]:
    text = _split_tables(text, _page_markers(text), grids)
    marks = _page_markers(text)
    buckets: dict[int, list[str]] = {marks[0][1]: [text[: marks[0][0].start()]]}
    for k, (mark, page) in enumerate(marks):
        seg = text[mark.end() : marks[k + 1][0].start() if k + 1 < len(marks) else len(text)]
        buckets.setdefault(page, []).append(seg)
    return [(i, "\n\n".join(buckets.get(i, []))) for i in range(1, max(buckets) + 1)]


def parse_di_markdown(text: str) -> DiDocument:
    """解析 DI markdown 全文 → DiDocument。契约见模块 docstring。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stack: list[tuple[int, str]] = []
    pages: list[DiPage] = []
    tables: dict[str, TableGrid] = {}
    split = (
        _marker_pages(text, tables)
        if has_page_markers(text)
        else enumerate(_PAGE_BREAK.split(text), 1)
    )
    for index, raw in split:
        meta = _Meta()
        body = _INLINE_COMMENT.sub(meta.take, _LINE_COMMENT.sub(meta.take, raw))
        number, label = _page_number(meta.page_numbers, index)
        pages.append(
            DiPage(
                index=index,
                number=number,
                page_number_label=label,
                headers=tuple(meta.headers),
                footers=tuple(meta.footers),
                blocks=tuple(_blocks(body, stack, tables)),
            )
        )
    return DiDocument(pages=tuple(pages))
