"""检索期附页图：在已过 RESTRICTED 出口的检索结果上，给前 N 条（按页去重后即前 N 页）附页图引用。

``PageImageRetriever`` 是 ``NarrativeRetriever`` 包装（CorrectiveRetriever 同款写法）：只调用
``base.retrieve``，拿到的结果已经被 link / rerank 两个出口剔掉了 RESTRICTED 块；它只给前 ``top_n``
条加一个 ``page_image`` 键（其余键原样不动，文本与引用字段不变）::

    {"path": "<绝对路径>", "doc_id": "...", "page": N, "image_sha256": "...", "pdf_sha256": "..."}

页图是整页内容，比出口处理的单个块范围更大，所以这里是**新出口，门口再筛一次**：只要该页在块库里有任何一个
活跃的 RESTRICTED 块，就不发这一页的图（即使映射表里有），跳过原因 ``restricted``。其他跳过原因：
``no_page``（locator 没有 ``@page=N``）、``no_image``（没有关联 PDF / 该页没渲染）、``missing_file``
（映射行在但文件丢了）。

要求页级父子开关（``page_parent``）是 ``dedup`` 或 ``page+child``：此时每条结果是不同的页、上下文是整页文本，
页图与整页文本一一对应。``off`` 时同一页可能以多个小块出现，附图会重复且图文粒度不一致，所以一张都不附，
trace 记 ``reason=page_parent_off``。

默认 ``RAGSPINE_PAGE_IMAGES=off``：``make_page_image_retriever`` 原样返回 base，检索与 prompt 逐字节不变。
trace（op=narrative.page_images）只记计数与原因代码，不记路径、不记内容。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ragspine.common.observability import emit_trace
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.page_images.store import PageImageStore
from ragspine.retrieval.page_parent.pages import (
    PAGE_PARENT_OFF,
    is_restricted,
    make_page_parent_mode,
    page_key,
)

PAGE_IMAGES_OFF = "off"
PAGE_IMAGES_ON = "on"
PAGE_IMAGES_MODES = (PAGE_IMAGES_OFF, PAGE_IMAGES_ON)
DEFAULT_PAGE_IMAGES_TOP_N = 3
PAGE_IMAGE_KEY = "page_image"

SKIP_NO_PAGE = "no_page"
SKIP_RESTRICTED = "restricted"
SKIP_NO_IMAGE = "no_image"
SKIP_MISSING_FILE = "missing_file"
REASON_PAGE_PARENT_OFF = "page_parent_off"


def make_page_images_mode(spec: str | None) -> str:
    """开关值归一：None / '' / 'none' / 'off' → 'off'；'on'。未知值抛 ValueError。"""
    value = (spec or "").strip().lower()
    if value in ("", "none", PAGE_IMAGES_OFF):
        return PAGE_IMAGES_OFF
    if value == PAGE_IMAGES_ON:
        return PAGE_IMAGES_ON
    raise ValueError(f"未知 page_images 取值 {spec!r}，可选 {PAGE_IMAGES_MODES}")


class PageImageRetriever:
    """给 base 检索结果的前 ``top_n`` 条附页图引用（每次检索自开自闭 sqlite 连接）。"""

    def __init__(
        self,
        base: Any,
        *,
        chunk_db_path: str | Path,
        image_dir: str | Path | None = None,
        top_n: int = DEFAULT_PAGE_IMAGES_TOP_N,
        page_parent: str | None = None,
    ) -> None:
        if top_n < 0:
            raise ValueError("top_n 必须 >= 0")
        self.base = base
        self.chunk_db_path = str(chunk_db_path)
        self.image_dir = image_dir
        self.top_n = top_n
        self.page_parent = make_page_parent_mode(page_parent)

    def retrieve(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 50
    ) -> list[dict[str, Any]]:
        snippets = list(self.base.retrieve(query, filters=filters, top_k=top_k))
        if self.page_parent == PAGE_PARENT_OFF:
            self._trace(0, {}, reason=REASON_PAGE_PARENT_OFF)
            return snippets
        head = snippets[: self.top_n]
        skips: dict[str, int] = {}
        attached = 0
        images = PageImageStore(self.chunk_db_path, self.image_dir)
        chunks = ChunkStore(self.chunk_db_path)
        restricted_by_doc: dict[str, set[int]] = {}
        try:
            for i, snippet in enumerate(head):
                ref, reason = self._lookup(snippet, images, chunks, restricted_by_doc)
                if ref is None:
                    skips[reason] = skips.get(reason, 0) + 1
                    continue
                snippets[i] = {**snippet, PAGE_IMAGE_KEY: ref}
                attached += 1
        finally:
            chunks.close()
            images.close()
        self._trace(attached, skips, n_candidates=len(head))
        return snippets

    def _lookup(
        self,
        snippet: dict[str, Any],
        images: PageImageStore,
        chunks: ChunkStore,
        restricted_by_doc: dict[str, set[int]],
    ) -> tuple[dict[str, Any] | None, str]:
        key = page_key(
            SimpleNamespace(
                doc_id=snippet.get("doc_id", ""),
                source_locator=snippet.get("source_locator", ""),
            )
        )
        if key is None:
            return None, SKIP_NO_PAGE
        doc_id, page = key
        if doc_id not in restricted_by_doc:
            restricted_by_doc[doc_id] = {
                k[1]
                for c in chunks.iter_chunks(doc_id=doc_id)
                if is_restricted(c) and (k := page_key(c)) is not None
            }
        if page in restricted_by_doc[doc_id]:
            return None, SKIP_RESTRICTED
        image = images.get(doc_id, page)
        if image is None:
            return None, SKIP_NO_IMAGE
        if not image.path.is_file():
            return None, SKIP_MISSING_FILE
        return {
            "path": str(image.path.resolve()),
            "doc_id": doc_id,
            "page": page,
            "image_sha256": image.image_sha256,
            "pdf_sha256": image.pdf_sha256,
        }, ""

    def _trace(
        self, attached: int, skips: dict[str, int], *, n_candidates: int = 0, reason: str = ""
    ) -> None:
        emit_trace(
            None,
            op="narrative.page_images",
            page_parent=self.page_parent,
            top_n=self.top_n,
            n_candidates=n_candidates,
            n_attached=attached,
            n_skipped=sum(skips.values()),
            skip_reasons=dict(sorted(skips.items())),
            reason=reason,
        )


def make_page_image_retriever(
    base: Any,
    spec: str | None,
    *,
    chunk_db_path: str | Path,
    image_dir: str | Path | None = None,
    top_n: int = DEFAULT_PAGE_IMAGES_TOP_N,
    page_parent: str | None = None,
) -> Any:
    """``off``（默认）原样返回 base；``on`` 包成 :class:`PageImageRetriever`。"""
    if make_page_images_mode(spec) == PAGE_IMAGES_OFF:
        return base
    return PageImageRetriever(
        base,
        chunk_db_path=chunk_db_path,
        image_dir=image_dir,
        top_n=top_n,
        page_parent=page_parent,
    )
