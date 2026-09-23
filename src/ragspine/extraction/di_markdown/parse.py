"""Azure Document Intelligence 风格 markdown → `DiDocument`（页 → 块）。

格式依据：Microsoft Learn「Document Intelligence supported Markdown elements」
（prebuilt-layout, `outputContentFormat=markdown`）。约定与本解析器的取法：

- **分页**：`<!-- PageBreak -->` 为页间分隔符，页数 = 分隔符数 + 1（空输入 = 1 个空页）。
  `DiPage.index` 为物理页序（1 起）。
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

from ragspine.extraction.di_markdown.html_table import parse_html_table
from ragspine.extraction.di_markdown.models import (
    Block,
    DiDocument,
    DiPage,
    Figure,
    Heading,
    Paragraph,
    Table,
)

_PAGE_BREAK = re.compile(r"<!--\s*PageBreak\s*-->")
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


def _blocks(body: str, stack: list[tuple[int, str]]) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    def path() -> tuple[str, ...]:
        return tuple(text for _, text in stack)

    def flush() -> None:
        if para:
            blocks.append(Paragraph(text="\n".join(para), heading_path=path()))
            para.clear()

    pos, n = 0, len(body)
    while pos < n:
        eol = body.find("\n", pos)
        eol = n if eol < 0 else eol
        line = body[pos:eol]
        stripped = line.strip()
        lead = pos + len(line) - len(line.lstrip())
        heading = _HEADING.match(line)
        if not stripped:
            flush()
            pos = eol + 1
        elif heading is not None:
            flush()
            level = len(heading.group(1))
            text = html.unescape(_CLOSING_HASHES.sub("", heading.group(2) or "").strip())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            blocks.append(Heading(text=text, level=level, heading_path=path()))
            pos = eol + 1
        elif _TABLE_OPEN.match(stripped):
            flush()
            end = _table_end(body, lead)
            blocks.append(Table(grid=parse_html_table(body[lead:end]), heading_path=path()))
            pos = end
        elif (opener := _FIGURE_OPEN.match(body, lead)) is not None:
            flush()
            close = _FIGURE_CLOSE.search(body, opener.end())
            inner_end, pos = (close.start(), close.end()) if close else (n, n)
            blocks.append(_figure(body[opener.end() : inner_end], path()))
        else:
            para.append(html.unescape(stripped))
            pos = eol + 1
    flush()
    return blocks


def parse_di_markdown(text: str) -> DiDocument:
    """解析 DI markdown 全文 → DiDocument。契约见模块 docstring。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stack: list[tuple[int, str]] = []
    pages: list[DiPage] = []
    for index, raw in enumerate(_PAGE_BREAK.split(text), start=1):
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
                blocks=tuple(_blocks(body, stack)),
            )
        )
    return DiDocument(pages=tuple(pages))
