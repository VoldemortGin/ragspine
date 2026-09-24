"""页级父子的开关解析与页标识。

页标识取自块的 ``source_locator``：按段切块（T2）写入的 locator 形如 ``{doc_id}@page=N#paraX-Y``，
页码是入库时就定下的结构化定位（``chunk_segments`` 的段前缀），这里只做一次锚定正则解析；doc_id 用块自己的
``doc_id`` 字段。没有 ``@page=N`` 的块（旧式整篇切块的 PDF 叙事、pptx 的 slide= 等）没有页标识，原样保留、
不参与去重。
"""

import re
from collections.abc import Iterable
from typing import Any

PAGE_PARENT_OFF = "off"
PAGE_PARENT_DEDUP = "dedup"
PAGE_PARENT_PAGE_CHILD = "page+child"
PAGE_PARENT_MODES = (PAGE_PARENT_OFF, PAGE_PARENT_DEDUP, PAGE_PARENT_PAGE_CHILD)

_RESTRICTED = "RESTRICTED"
_PAGE_RE = re.compile(r"@page=(\d+)(?:#|$)")


def make_page_parent_mode(spec: str | None) -> str:
    """开关值归一：None / '' / 'none' / 'off' → 'off'；'dedup'；'page+child'。未知值抛 ValueError。"""
    value = (spec or "").strip().lower()
    if value in ("", "none", PAGE_PARENT_OFF):
        return PAGE_PARENT_OFF
    if value in PAGE_PARENT_MODES:
        return value
    raise ValueError(f"未知 page_parent 取值 {spec!r}，可选 {PAGE_PARENT_MODES}")


def is_restricted(chunk: Any) -> bool:
    """与 link / rerank 两个出口同一口径（大小写不敏感）。"""
    return str(getattr(chunk, "sensitivity", "")).upper() == _RESTRICTED


def page_key(chunk: Any) -> tuple[str, int] | None:
    """块的页标识 (doc_id, page)；locator 里没有 ``@page=N`` 时为 None。"""
    match = _PAGE_RE.search(str(getattr(chunk, "source_locator", "")))
    return (str(chunk.doc_id), int(match.group(1))) if match else None


def page_locator(chunk: Any) -> str:
    """页级 locator：代表块精确 locator 的页前缀（'{doc_id}@page=N'）；无页码时为 ''。"""
    if page_key(chunk) is None:
        return ""
    return str(chunk.source_locator).split("#", 1)[0]


def group_pages[T](chunks: Iterable[T]) -> dict[tuple[str, int], list[T]]:
    """按页分组非 RESTRICTED 的带页码块（组内按 seq 排序）；RESTRICTED 块绝不进入任何页组。"""
    groups: dict[tuple[str, int], list[T]] = {}
    for chunk in chunks:
        key = page_key(chunk)
        if key is not None and not is_restricted(chunk):
            groups.setdefault(key, []).append(chunk)
    for members in groups.values():
        members.sort(key=lambda c: getattr(c, "seq", 0))
    return groups


def page_heading(chunks: Iterable[Any]) -> str:
    """整页单元的标题：页内各块标题路径（" > " 分段）去重保序后拼一次；都没有标题 -> ''。

    只给整页单元的【索引文本】用（标题进索引开关打开时），每个标题段只出现一次，
    不按块重复、不放大 BM25 词频。调用方只传同页的非 RESTRICTED 块（group_pages 的组）。
    """
    segments: dict[str, None] = {}
    for chunk in chunks:
        for segment in str(getattr(chunk, "heading", "") or "").split(" > "):
            if segment.strip():
                segments.setdefault(segment.strip(), None)
    return " > ".join(segments)
