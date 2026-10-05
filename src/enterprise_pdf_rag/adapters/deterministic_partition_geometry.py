"""确定性版面切分的纯几何工具: 只依赖 stdlib 与 ``TextSpan``, 不做任何 I/O.

ADR 00NN(deterministic-text-page-partition)的纯函数半边: 聚行 / 跨页重复行 / 页眉页脚带 /
栏式判定 / 标题-段落-列表分块. 原则沿用 ADR 0013 修订 1: 读不懂的版面返回 ``ambiguous``,
由调用方回退到模型版面, 绝不去猜.

注意: 本模块与主仓库 ``ragspine.extraction.evidence.page.text_lines``(另一分支)的
``LINE_OVERLAP`` / ``RUNNING_BUCKET`` / ``RUNNING_SHARE`` / ``RunningKey`` / ``TextLine`` /
``text_lines`` / ``running_lines`` 语义对齐, 集成时去重为 import.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Literal

from ragspine.extraction.evidence.document.models import TextSpan

# -- 聚行: 两个 span 竖直重叠达到较矮者的一半即视为同一行(与模型版面的行观感一致).
LINE_OVERLAP = 0.5
# -- 跨页重复行: 同文本同高度桶出现在 >=30% 且 >=2 页, 视为页眉/页脚/装饰行.
RUNNING_SHARE = 0.3
MIN_RUNNING_PAGES = 2
# -- 高度桶宽(pt): 同一条页眉在不同页的 y 浮动小于半个桶即落入同桶.
RUNNING_BUCKET = 5.0
# -- 页眉/页脚带: 页高顶部/底部各 6%(调查: AIA 样本与常见财报页边距均在此内).
HEADER_BAND_SHARE = 0.06
FOOTER_BAND_SHARE = 0.06
# -- 栏沟: 贯穿正文、无任何 span 跨越的竖直空白至少 18pt(约两个字符宽)才算候选栏沟.
MIN_GUTTER_WIDTH = 18.0
# -- 行对齐判定: 触沟行中两侧都有内容的占比 >= 0.8 → 整页按"行"读(标签/数值式版面).
ROW_ALIGNED_SHARE = 0.8
# -- 双栏判定: 两侧内容各自成行、跨沟行占比 <= 0.2 → 真双栏, 先左栏后右栏.
COLUMN_DISJOINT_SHARE = 0.2
# -- 行读模式下至少一侧的行段中位字符数不超过此值(叙事栏通常 >40 字符), 否则拿不准.
ROWS_MAX_MEDIAN_SEGMENT_CHARS = 24
# -- 标题: 行内最大字号达到正文众数字号的 1.15 倍.
HEADING_SIZE_RATIO = 1.15
# -- 段落断行: 相邻行竖直间距超过行字号的 0.9 倍视为块间隙(用于列表收尾).
PARAGRAPH_GAP_FACTOR = 0.9
# -- 列表项续行必须比项目符号行首至少右缩进 4pt.
LIST_INDENT_TOLERANCE = 4.0
# -- 单个项目符号不成列表(拿不准就出 Text).
MIN_LIST_ITEMS = 2

type RunningKey = tuple[str, int]

# 常见项目符号, 含 en/em dash 与连字符(真连字符列表项以 "- " 开头).
_BULLET_SYMBOLS = "•◦▪●○‣·–—-*"  # noqa: RUF001
_NUMBER_MARKER = re.compile(r"^\(?\d{1,3}[.)]\s+")
_PAGE_NUMBER = re.compile(
    r"^(?:-\s*)?(?:page\s+)?\d{1,3}(?:\s*(?:/|of)\s*\d{1,4})?(?:\s*-)?$",
    re.IGNORECASE,
)
_CJK_PAGE_NUMBER = re.compile(r"^第?\s*\d{1,3}\s*页$")


@dataclass(frozen=True, slots=True)
class TextLine:
    """按竖直重叠聚出的一行, 行内 span 已按 x 排序."""

    spans: tuple[TextSpan, ...]

    @property
    def text(self) -> str:
        return " ".join(span.text.strip() for span in self.spans if span.text.strip())

    @property
    def span_ids(self) -> tuple[str, ...]:
        return tuple(span.span_id for span in self.spans)

    @property
    def top(self) -> float:
        return min(span.bbox[1] for span in self.spans)

    @property
    def bottom(self) -> float:
        return max(span.bbox[3] for span in self.spans)

    @property
    def left(self) -> float:
        return min(span.bbox[0] for span in self.spans)

    @property
    def right(self) -> float:
        return max(span.bbox[2] for span in self.spans)

    @property
    def size(self) -> float:
        return max(span.size for span in self.spans)

    @property
    def key(self) -> RunningKey:
        return (self.text, round(self.top / RUNNING_BUCKET))


def text_lines(spans: Iterable[TextSpan]) -> tuple[TextLine, ...]:
    """按竖直重叠 >= 较矮者一半聚行; 行内按 x、行间按 top 排序."""
    groups: list[list[TextSpan]] = []
    bands: list[tuple[float, float]] = []
    for span in sorted(spans, key=lambda item: (item.bbox[1], item.bbox[0])):
        top, bottom = span.bbox[1], span.bbox[3]
        height = max(bottom - top, 0.0)
        placed = False
        for index, (band_top, band_bottom) in enumerate(bands):
            overlap = min(bottom, band_bottom) - max(top, band_top)
            shorter = max(min(height, band_bottom - band_top), 1e-9)
            if overlap >= LINE_OVERLAP * shorter:
                groups[index].append(span)
                bands[index] = (min(band_top, top), max(band_bottom, bottom))
                placed = True
                break
        if not placed:
            groups.append([span])
            bands.append((top, bottom))
    lines = [
        TextLine(tuple(sorted(group, key=lambda item: (item.bbox[0], item.bbox[1]))))
        for group in groups
    ]
    return tuple(sorted(lines, key=lambda line: (line.top, line.left)))


def running_lines(pages: Sequence[tuple[float, Iterable[TextSpan]]]) -> frozenset[RunningKey]:
    """同文本同高度桶出现在 >=30% 且 >=2 页的行键(页眉/页脚/重复装饰行)."""
    counts: dict[RunningKey, int] = {}
    for _page_height, spans in pages:
        for key in {line.key for line in text_lines(spans)}:
            counts[key] = counts.get(key, 0) + 1
    needed = max(MIN_RUNNING_PAGES, -(-len(pages) * 3 // 10))  # ceil(0.3 * pages)
    return frozenset(key for key, count in counts.items() if count >= needed)


def is_page_number(text: str) -> bool:
    """页码形状: 纯 1-3 位数字(4 位是年份)、Page N、N / M、- N -、第 N 页."""
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_PAGE_NUMBER.fullmatch(stripped)) or bool(_CJK_PAGE_NUMBER.fullmatch(stripped))


@dataclass(frozen=True, slots=True)
class MarginSplit:
    """页眉带 / 正文 / 页脚带的行划分; 页边带里只有跨页重复行与页码出带."""

    header: tuple[TextLine, ...]
    body: tuple[TextLine, ...]
    footer: tuple[TextLine, ...]


def split_margin_lines(
    lines: Sequence[TextLine], *, page_height: float, running: frozenset[RunningKey]
) -> MarginSplit:
    """顶/底带内的跨页重复行与页码单独成页眉/页脚行, 其余一律正文(拿不准就留正文)."""
    header: list[TextLine] = []
    body: list[TextLine] = []
    footer: list[TextLine] = []
    top_band = page_height * HEADER_BAND_SHARE
    bottom_band = page_height * (1.0 - FOOTER_BAND_SHARE)
    for line in lines:
        margin = line.key in running or is_page_number(line.text)
        if margin and line.bottom <= top_band:
            header.append(line)
        elif margin and line.top >= bottom_band:
            footer.append(line)
        else:
            body.append(line)
    return MarginSplit(tuple(header), tuple(body), tuple(footer))


def gutters(lines: Sequence[TextLine]) -> tuple[tuple[float, float], ...]:
    """贯穿正文 x 范围、无 span 跨越、宽 >= 18pt 的竖直空白区间."""
    intervals = sorted((span.bbox[0], span.bbox[2]) for line in lines for span in line.spans)
    if not intervals:
        return ()
    merged: list[tuple[float, float]] = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return tuple(
        (merged[index][1], merged[index + 1][0])
        for index in range(len(merged) - 1)
        if merged[index + 1][0] - merged[index][1] >= MIN_GUTTER_WIDTH
    )


@dataclass(frozen=True, slots=True)
class ColumnLayout:
    """正文读法: ``rows``(按行) / ``columns``(先左栏后右栏, ``boundary`` 为分界 x) /
    ``ambiguous``(拿不准, 调用方必须回退模型版面)."""

    mode: Literal["rows", "columns", "ambiguous"]
    boundary: float | None


def _segment_chars(lines: Sequence[TextLine], low: float, high: float) -> list[int]:
    counts = []
    for line in lines:
        text = " ".join(
            span.text.strip()
            for span in line.spans
            if low <= (span.bbox[0] + span.bbox[2]) / 2 < high
        )
        if text.strip():
            counts.append(len(text))
    return counts


def column_layout(lines: Sequence[TextLine]) -> ColumnLayout:
    """无栏沟按行读; 一条栏沟时区分行对齐(按行)与真双栏(按栏); 其余 ``ambiguous``."""
    gaps = gutters(lines)
    if not gaps:
        return ColumnLayout("rows", None)
    aligned_everywhere = True
    for low, high in gaps:
        touching = both = 0
        for line in lines:
            left = any(span.bbox[2] <= low + 1e-6 for span in line.spans)
            right = any(span.bbox[0] >= high - 1e-6 for span in line.spans)
            if left or right:
                touching += 1
            if left and right:
                both += 1
        if touching == 0:
            continue
        share = both / touching
        if share >= ROW_ALIGNED_SHARE:
            left_chars = _segment_chars(lines, float("-inf"), (low + high) / 2)
            right_chars = _segment_chars(lines, (low + high) / 2, float("inf"))
            narrow = min(
                median(left_chars) if left_chars else 0.0,
                median(right_chars) if right_chars else 0.0,
            )
            if narrow > ROWS_MAX_MEDIAN_SEGMENT_CHARS:
                # 两侧都是叙事宽行却行行对齐: 可能是基线恰好对齐的双栏, 拿不准.
                return ColumnLayout("ambiguous", None)
            continue
        if share <= COLUMN_DISJOINT_SHARE and len(gaps) == 1:
            aligned_everywhere = False
            continue
        return ColumnLayout("ambiguous", None)
    if aligned_everywhere:
        return ColumnLayout("rows", None)
    low, high = gaps[0]
    return ColumnLayout("columns", (low + high) / 2)


def bullet_marker(text: str) -> Literal["symbol", "number"] | None:
    """行首项目符号: 单个符号 + 空白为 ``symbol``, ``1.`` / ``12)`` 式编号为 ``number``."""
    stripped = text.lstrip()
    if len(stripped) >= 2 and stripped[0] in _BULLET_SYMBOLS and stripped[1] == " ":
        return "symbol"
    if _NUMBER_MARKER.match(stripped):
        return "number"
    return None


def body_font_size(lines: Sequence[TextLine]) -> float:
    """按字符数加权的正文字号众数; 没有正文时为 0."""
    weights: dict[float, int] = {}
    for line in lines:
        for span in line.spans:
            chars = len(span.text.strip())
            if chars and span.size > 0:
                weights[span.size] = weights.get(span.size, 0) + chars
    if not weights:
        return 0.0
    return max(weights, key=lambda size: (weights[size], -size))


def _is_heading(line: TextLine, body_size: float) -> bool:
    if body_size <= 0:
        return False
    return line.size >= body_size * HEADING_SIZE_RATIO


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """一个待建版面对象: ``text``(标题与其后段落合并) 或 ``list``(分项)."""

    kind: Literal["text", "list"]
    lines: tuple[TextLine, ...]
    items: tuple[tuple[TextLine, ...], ...] = ()
    ordered: bool = False


def body_blocks(lines: Sequence[TextLine], *, body_size: float) -> tuple[BlockSpec, ...]:
    """标题开启新 Text 并吞并其后段落; 连续项目符号成 List; 拿不准一律 Text."""
    blocks: list[BlockSpec] = []
    text_run: list[TextLine] = []
    items: list[list[TextLine]] = []
    markers: list[str] = []

    def flush_text() -> None:
        if text_run:
            blocks.append(BlockSpec("text", tuple(text_run)))
            text_run.clear()

    def flush_list() -> None:
        if not items:
            return
        if len(items) < MIN_LIST_ITEMS:
            # 单个项目符号拿不准: 并回 Text.
            text_run.extend(line for item in items for line in item)
            flush_text()
        else:
            blocks.append(
                BlockSpec(
                    "list",
                    tuple(line for item in items for line in item),
                    tuple(tuple(item) for item in items),
                    ordered=all(marker == "number" for marker in markers),
                )
            )
        items.clear()
        markers.clear()

    previous: TextLine | None = None
    for line in lines:
        marker = bullet_marker(line.text)
        heading = _is_heading(line, body_size)
        gap_break = (
            previous is not None
            and line.top - previous.bottom
            > PARAGRAPH_GAP_FACTOR * max(line.size, previous.size, 1.0)
        )
        if items:
            if marker is not None and not heading:
                items.append([line])
                markers.append(marker)
            elif (
                not heading
                and not gap_break
                and line.left >= items[-1][0].left + LIST_INDENT_TOLERANCE
            ):
                items[-1].append(line)
            else:
                flush_list()
                if marker is not None and not heading:
                    items.append([line])
                    markers.append(marker)
                else:
                    if heading:
                        flush_text()
                    text_run.append(line)
        elif marker is not None and not heading:
            flush_text()
            items.append([line])
            markers.append(marker)
        else:
            if heading:
                flush_text()
            text_run.append(line)
        previous = line
    flush_list()
    flush_text()
    return tuple(blocks)
