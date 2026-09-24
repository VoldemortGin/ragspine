"""页标签：从一页 DI markdown 的块算原始度量，再按阈值得出标签（页图按需附图的触发依据，ADR 0025）。

入库时只存原始度量（:class:`PageTagStats`），标签在检索时按当前阈值现算，改阈值不用重新入库：

- ``has_table``：页内有 ``Table`` 块；``Paragraph`` 文本里有 pipe 表分隔行（至少含一个 ``|``）也算，
  作为非 DI markdown 的兜底。单独一行 ``---``（水平线）不算。
- ``has_figure``：页内有 ``Figure`` 块，且最大那个图的文字量（text + caption 去空白后的字符数）
  ≥ ``figure_min_chars``。logo、装饰图在 DI 里也会标成 figure，但几乎没有文字，靠这个阈值滤掉。
- ``low_text``：各块文字（标题、段落、表格锚点格、图的 text 与 caption）拼起来，去掉空白和 ``|`` 后
  不足 ``low_text_chars`` 个字符。页眉、页脚、页码和其他注释已被 ``parse.py`` 剥离，不计入。

纯函数、只用 stdlib。页号用物理页序（``DiPage.index``），与块 locator 的 ``page=N`` 一致。
"""

import re
from dataclasses import dataclass

from ragspine.extraction.di_markdown.models import (
    DiDocument,
    DiPage,
    Figure,
    Heading,
    Paragraph,
    Table,
)

TAG_HAS_TABLE = "has_table"
TAG_HAS_FIGURE = "has_figure"
TAG_LOW_TEXT = "low_text"
PAGE_TAG_NAMES = (TAG_HAS_TABLE, TAG_HAS_FIGURE, TAG_LOW_TEXT)
DEFAULT_LOW_TEXT_CHARS = 300
DEFAULT_FIGURE_MIN_CHARS = 10
# 度量口径的版本：口径一变，入库签名随之失效、重跑 ingest 即重算。
PAGE_TAGS_VERSION = 1

# SuperIndex 的 pipe 分隔行正则，外加「至少一个 |」：否则单独的 --- 水平线也会被当成表。
_PIPE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PageTagStats:
    """一页的原始度量（入库存这些，标签在检索时按阈值现算）。"""

    page: int
    has_table: bool
    n_figures: int
    figure_max_chars: int
    text_chars: int


def _chars(text: str) -> int:
    return len("".join(text.replace("|", " ").split()))


def _has_pipe_table(text: str) -> bool:
    return any("|" in m.group(0) for m in _PIPE_SEP.finditer(text))


def page_tag_stats(page: DiPage) -> PageTagStats:
    """一页的原始度量。"""
    has_table = False
    figure_sizes: list[int] = []
    text_chars = 0
    for block in page.blocks:
        if isinstance(block, Table):
            has_table = True
            text_chars += sum(_chars(cell.text) for cell in block.grid.cells)
            text_chars += _chars(block.grid.caption or "")
        elif isinstance(block, Figure):
            size = _chars(block.text) + _chars(block.caption or "")
            figure_sizes.append(size)
            text_chars += size
        elif isinstance(block, Paragraph):
            has_table = has_table or _has_pipe_table(block.text)
            text_chars += _chars(block.text)
        elif isinstance(block, Heading):
            text_chars += _chars(block.text)
    return PageTagStats(
        page=page.index,
        has_table=has_table,
        n_figures=len(figure_sizes),
        figure_max_chars=max(figure_sizes, default=0),
        text_chars=text_chars,
    )


def document_tag_stats(doc: DiDocument) -> tuple[PageTagStats, ...]:
    """整份文档逐页的原始度量（物理页序）。"""
    return tuple(page_tag_stats(page) for page in doc.pages)


def page_tags(
    stats: PageTagStats,
    *,
    low_text_chars: int = DEFAULT_LOW_TEXT_CHARS,
    figure_min_chars: int = DEFAULT_FIGURE_MIN_CHARS,
) -> frozenset[str]:
    """按阈值得出这一页的标签集合。阈值须 ≥0。"""
    if low_text_chars < 0 or figure_min_chars < 0:
        raise ValueError("low_text_chars / figure_min_chars 须 >= 0")
    tags: set[str] = set()
    if stats.has_table:
        tags.add(TAG_HAS_TABLE)
    if stats.n_figures and stats.figure_max_chars >= figure_min_chars:
        tags.add(TAG_HAS_FIGURE)
    if stats.text_chars < low_text_chars:
        tags.add(TAG_LOW_TEXT)
    return frozenset(tags)
