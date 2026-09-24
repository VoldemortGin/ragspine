"""页图触发策略：在 ``PageImageRetriever`` 外面包一层，决定前 N 页里哪几页真的附图（ADR 0025）。

模式（``RAGSPINE_PAGE_IMAGES``）：

- ``off``（默认）：原样返回 base，prompt 逐字节不变。
- ``all``（``on`` 是它的别名）：前 ``top_n`` 页全附。不设上限时**直接返回** ``PageImageRetriever``，
  与引入本策略之前的 ``on`` 逐字节一致；设了上限才包一层去重 + 截断。
- ``tagged``：只给命中触发标签（``RAGSPINE_PAGE_IMAGES_TRIGGER``，逗号分隔、之间为「或」，``any`` = 全部）
  的页附图。标签来自 ``page_tag`` 表（旧库懒算，见 ``tag_store.py``）；查不到标签的 doc 一张不附（``untagged``）。

选择算法：按名次遍历 base 结果里带 ``page_image`` 的条目，同一 ``(doc_id, page)`` 或同一 ``image_sha256``
只留第一次（``dup``）；tagged 模式再去掉不满足条件的（``not_tagged`` / ``untagged``）；最后按名次截到
``max_images``（``over_max``；不设 = ``top_n``）。

**只删不增**：包装器只会把某条结果的 ``page_image`` 键去掉，从不新增引用、不改其他键，所以 RESTRICTED
的门口筛查（``attach.py``）原样继承。trace（op=narrative.page_image_trigger）只记计数与原因码。
"""

from pathlib import Path
from typing import Any

from ragspine.common.observability import emit_trace
from ragspine.extraction.di_markdown.page_tags import (
    DEFAULT_FIGURE_MIN_CHARS,
    DEFAULT_LOW_TEXT_CHARS,
    PAGE_TAG_NAMES,
    page_tags,
)
from ragspine.retrieval.page_images.attach import (
    DEFAULT_PAGE_IMAGES_TOP_N,
    PAGE_IMAGE_KEY,
    PAGE_IMAGES_ON,
    PageImageRetriever,
)
from ragspine.retrieval.page_images.trigger.tag_store import (
    DocTags,
    PageTagStore,
    load_doc_tags,
)

PAGE_IMAGES_OFF = "off"
PAGE_IMAGES_TAGGED = "tagged"
PAGE_IMAGES_ALL = "all"
PAGE_IMAGES_POLICIES = (PAGE_IMAGES_OFF, PAGE_IMAGES_TAGGED, PAGE_IMAGES_ALL)
DEFAULT_PAGE_IMAGES_TRIGGER = "has_table,low_text"
TRIGGER_ANY = "any"

DROP_DUP = "dup"
DROP_NOT_TAGGED = "not_tagged"
DROP_UNTAGGED = "untagged"
DROP_OVER_MAX = "over_max"


def make_page_images_policy(spec: str | None) -> str:
    """模式归一：None / '' / 'none' / 'off' → off；'on' / 'all' → all；'tagged'。未知值抛 ValueError。"""
    value = (spec or "").strip().lower()
    if value in ("", "none", PAGE_IMAGES_OFF):
        return PAGE_IMAGES_OFF
    if value in (PAGE_IMAGES_ON, PAGE_IMAGES_ALL):
        return PAGE_IMAGES_ALL
    if value == PAGE_IMAGES_TAGGED:
        return PAGE_IMAGES_TAGGED
    raise ValueError(
        f"未知 page_images 取值 {spec!r}，可选 {PAGE_IMAGES_POLICIES}（'on' 是 'all' 的别名）"
    )


def parse_page_image_trigger(spec: str) -> frozenset[str]:
    """逗号分隔的标签子集（取自 has_table / has_figure / low_text），或 'any'。空或未知标签抛 ValueError。"""
    names = [n.strip().lower() for n in (spec or "").split(",") if n.strip()]
    if not names:
        raise ValueError(f"page_images_trigger 不能为空，可选 {PAGE_TAG_NAMES} 或 'any'")
    if names == [TRIGGER_ANY]:
        return frozenset(PAGE_TAG_NAMES)
    unknown = sorted(set(names) - set(PAGE_TAG_NAMES))
    if unknown:
        raise ValueError(
            f"未知 page_images_trigger 标签 {unknown}，可选 {PAGE_TAG_NAMES} 或单独写 'any'"
        )
    return frozenset(names)


def _check_non_negative(**values: int | None) -> None:
    for name, value in values.items():
        if value is not None and value < 0:
            raise ValueError(f"{name} 必须 >= 0，收到 {value}")


class PageImageTriggerRetriever:
    """只删不增的页图筛选包装：去重、按标签过滤（tagged）、按名次截到上限。"""

    def __init__(
        self,
        base: Any,
        *,
        chunk_db_path: str | Path,
        mode: str = PAGE_IMAGES_TAGGED,
        trigger: str = DEFAULT_PAGE_IMAGES_TRIGGER,
        max_images: int = DEFAULT_PAGE_IMAGES_TOP_N,
        low_text_chars: int = DEFAULT_LOW_TEXT_CHARS,
        figure_min_chars: int = DEFAULT_FIGURE_MIN_CHARS,
    ) -> None:
        self.mode = make_page_images_policy(mode)
        if self.mode == PAGE_IMAGES_OFF:
            raise ValueError("PageImageTriggerRetriever 不接受 off（off 时应原样返回 base）")
        _check_non_negative(
            max_images=max_images, low_text_chars=low_text_chars, figure_min_chars=figure_min_chars
        )
        self.base = base
        self.chunk_db_path = str(chunk_db_path)
        self.trigger = parse_page_image_trigger(trigger)
        self.max_images = max_images
        self.low_text_chars = low_text_chars
        self.figure_min_chars = figure_min_chars

    def retrieve(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 50
    ) -> list[dict[str, Any]]:
        snippets = list(self.base.retrieve(query, filters=filters, top_k=top_k))
        drops: dict[str, int] = {}
        tag_sources: dict[str, int] = {}
        doc_tags: dict[str, DocTags | None] = {}
        seen_pages: set[tuple[object, object]] = set()
        seen_images: set[object] = set()
        n_candidates = kept = 0
        store: PageTagStore | None = None
        try:
            for i, snippet in enumerate(snippets):
                ref = snippet.get(PAGE_IMAGE_KEY)
                if not isinstance(ref, dict):
                    continue
                n_candidates += 1
                page_id = (ref.get("doc_id"), ref.get("page"))
                image_id = ref.get("image_sha256")
                reason = ""
                if page_id in seen_pages or (image_id and image_id in seen_images):
                    reason = DROP_DUP
                elif self.mode == PAGE_IMAGES_TAGGED:
                    if store is None:
                        store = PageTagStore(self.chunk_db_path)
                    reason = self._tag_reason(ref, store, doc_tags, tag_sources)
                seen_pages.add(page_id)
                if image_id:
                    seen_images.add(image_id)
                if not reason and kept >= self.max_images:
                    reason = DROP_OVER_MAX
                if reason:
                    drops[reason] = drops.get(reason, 0) + 1
                    snippets[i] = {k: v for k, v in snippet.items() if k != PAGE_IMAGE_KEY}
                else:
                    kept += 1
        finally:
            if store is not None:
                store.close()
        emit_trace(
            None,
            op="narrative.page_image_trigger",
            mode=self.mode,
            trigger=sorted(self.trigger),
            max_images=self.max_images,
            n_candidates=n_candidates,
            n_kept=kept,
            n_dropped=sum(drops.values()),
            drop_reasons=dict(sorted(drops.items())),
            tag_sources=dict(sorted(tag_sources.items())),
        )
        return snippets

    def _tag_reason(
        self,
        ref: dict[str, Any],
        store: PageTagStore,
        doc_tags: dict[str, DocTags | None],
        tag_sources: dict[str, int],
    ) -> str:
        doc_id = str(ref.get("doc_id") or "")
        if doc_id not in doc_tags:
            tags, source = load_doc_tags(store, doc_id)
            doc_tags[doc_id] = tags
            tag_sources[source] = tag_sources.get(source, 0) + 1
        tags = doc_tags[doc_id]
        stats = tags.get(int(ref.get("page") or 0)) if tags is not None else None
        if stats is None:
            return DROP_UNTAGGED
        hit = page_tags(
            stats, low_text_chars=self.low_text_chars, figure_min_chars=self.figure_min_chars
        )
        return "" if hit & self.trigger else DROP_NOT_TAGGED


def make_triggered_page_image_retriever(
    base: Any,
    spec: str | None,
    *,
    chunk_db_path: str | Path,
    image_dir: str | Path | None = None,
    top_n: int = DEFAULT_PAGE_IMAGES_TOP_N,
    page_parent: str | None = None,
    trigger: str = DEFAULT_PAGE_IMAGES_TRIGGER,
    max_images: int | None = None,
    low_text_chars: int = DEFAULT_LOW_TEXT_CHARS,
    figure_min_chars: int = DEFAULT_FIGURE_MIN_CHARS,
) -> Any:
    """装配：off 原样返回 base；all 且不设上限 = 原来的 ``PageImageRetriever``；其余再包 :class:`PageImageTriggerRetriever`。

    参数一律先校验（off 也校验），非法值抛 ValueError。
    """
    mode = make_page_images_policy(spec)
    parse_page_image_trigger(trigger)
    _check_non_negative(
        top_n=top_n,
        max_images=max_images,
        low_text_chars=low_text_chars,
        figure_min_chars=figure_min_chars,
    )
    if mode == PAGE_IMAGES_OFF:
        return base
    inner = PageImageRetriever(
        base,
        chunk_db_path=chunk_db_path,
        image_dir=image_dir,
        top_n=top_n,
        page_parent=page_parent,
    )
    if mode == PAGE_IMAGES_ALL and max_images is None:
        return inner
    return PageImageTriggerRetriever(
        inner,
        chunk_db_path=chunk_db_path,
        mode=mode,
        trigger=trigger,
        max_images=top_n if max_images is None else max_images,
        low_text_chars=low_text_chars,
        figure_min_chars=figure_min_chars,
    )
