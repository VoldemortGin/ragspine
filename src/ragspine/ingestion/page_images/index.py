"""入库期页图同步：按 doc 把原 PDF 渲染成页图，写进映射表（幂等）。

- 有 PDF：签名（PDF sha256 + dpi + max_side + RESTRICTED 页集合）不变即跳过；变了就重渲染并整体替换，
  旧的孤儿文件删除。
- 没有 PDF：撤下该 doc 以前的页图（关联由本次入库输入决定）；库里从没关联过时什么都不做、也不建表。
- 页标签（页图按需附图的触发依据，ADR 0025）：关联了 PDF 的 ``.md`` 同时逐页算原始度量写进 ``page_tag`` 表
  （签名 = markdown sha256 + 口径版本，不变即跳过）；撤下页图时一并撤下标签。trace op=narrative.page_tag_index 只记计数。
- RESTRICTED：页里只要有一个 RESTRICTED 块（按块库当前活跃块判断），这一页就不渲染、不落盘
  （``n_withheld`` 计数）；检索期出口还会再查一次，见 ``retrieval/page_images/attach.py``。
- trace 只记计数（op=narrative.page_image_index），不记路径和内容。
"""

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ragspine.common.observability import emit_trace
from ragspine.ingestion.narrative.narrative_ingest import (
    STATUS_INGESTED,
    STATUS_SKIPPED,
    NarrativeIngestReport,
)
from ragspine.ingestion.page_images.render import (
    DEFAULT_PAGE_IMAGE_DPI,
    DEFAULT_PAGE_IMAGE_MAX_SIDE,
    render_pdf_pages,
)
from ragspine.ingestion.page_images.source_pdf import MARKDOWN_SUFFIX, SourcePdf
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.page_images.store import PageImageStore
from ragspine.retrieval.page_images.trigger.tag_store import (
    PageTagStore,
    compute_markdown_tags,
    tag_signature,
)
from ragspine.retrieval.page_parent.pages import is_restricted, page_key

STATUS_RENDERED = "rendered"
STATUS_UNCHANGED = "unchanged"
STATUS_CLEARED = "cleared"
STATUS_DRY_RUN = "dry_run"
_STATUSES = (STATUS_RENDERED, STATUS_UNCHANGED, STATUS_CLEARED, STATUS_DRY_RUN)


@dataclass(frozen=True)
class PageImageDocReport:
    doc_id: str
    status: str
    pdf_sha256: str = ""
    n_pages: int = 0
    n_images: int = 0
    n_withheld: int = 0


@dataclass
class PageImageReport:
    docs: list[PageImageDocReport] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(_STATUSES, 0)
        for doc in self.docs:
            out[doc.status] += 1
        return out


def restricted_pages(chunk_db_path: str | Path, doc_id: str) -> set[int]:
    """块库里该 doc 活跃块中含 RESTRICTED 块的物理页。"""
    store = ChunkStore(chunk_db_path)
    try:
        store.init_schema()
        return {
            key[1]
            for chunk in store.iter_chunks(doc_id=doc_id)
            if is_restricted(chunk) and (key := page_key(chunk)) is not None
        }
    finally:
        store.close()


def sync_page_images(
    chunk_db_path: str | Path,
    sources: Mapping[str, SourcePdf | None],
    *,
    image_dir: str | Path | None = None,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
    max_side: int = DEFAULT_PAGE_IMAGE_MAX_SIDE,
    dry_run: bool = False,
) -> PageImageReport:
    """把 ``{doc_id: SourcePdf | None}`` 同步进页图映射表。"""
    report = PageImageReport()
    store = PageImageStore(chunk_db_path, image_dir)
    try:
        for doc_id, source in sources.items():
            doc_report = _sync_one(store, chunk_db_path, doc_id, source, dpi, max_side, dry_run)
            if doc_report is not None:
                report.docs.append(doc_report)
    finally:
        store.close()
    if report.docs:
        emit_trace(
            None,
            op="narrative.page_image_index",
            dpi=dpi,
            max_side=max_side,
            n_images=sum(d.n_images for d in report.docs),
            n_withheld=sum(d.n_withheld for d in report.docs),
            **report.counts(),
        )
    return report


def _sync_one(
    store: PageImageStore,
    chunk_db_path: str | Path,
    doc_id: str,
    source: SourcePdf | None,
    dpi: int,
    max_side: int,
    dry_run: bool,
) -> PageImageDocReport | None:
    if source is None:
        if dry_run or store.doc_signature(doc_id) is None:
            return None
        store.clear_doc(doc_id)
        return PageImageDocReport(doc_id=doc_id, status=STATUS_CLEARED)

    withheld = restricted_pages(chunk_db_path, doc_id) & set(range(1, source.page_count + 1))
    wanted = [p for p in range(1, source.page_count + 1) if p not in withheld]
    base = PageImageDocReport(
        doc_id=doc_id,
        status=STATUS_DRY_RUN,
        pdf_sha256=source.sha256,
        n_pages=source.page_count,
        n_images=len(wanted),
        n_withheld=len(withheld),
    )
    if dry_run:
        return base

    signature = hashlib.sha256(
        f"{source.sha256}|{dpi}|{max_side}|{sorted(withheld)}".encode()
    ).hexdigest()
    if store.doc_signature(doc_id) == signature:
        return replace(base, status=STATUS_UNCHANGED)

    rendered = render_pdf_pages(source.path, dpi=dpi, max_side=max_side, pages=wanted)
    n = store.replace_doc(
        doc_id,
        pdf_sha256=source.sha256,
        pdf_pages=source.page_count,
        dpi=dpi,
        max_side=max_side,
        signature=signature,
        pages=rendered,
    )
    return replace(base, status=STATUS_RENDERED, n_images=n)


def sync_ingested_page_images(
    report: NarrativeIngestReport,
    sources: Mapping[str, SourcePdf],
    chunk_db_path: str | Path,
    *,
    image_dir: str | Path | None = None,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
    max_side: int = DEFAULT_PAGE_IMAGE_MAX_SIDE,
) -> PageImageReport:
    """叙事入库之后调用：本批成功入库（含幂等跳过）的每个 .md 按 ``sources`` 同步页图；没配 PDF 的撤下旧图。"""
    files = [
        f
        for f in report.files
        if Path(f.path).suffix.lower() == MARKDOWN_SUFFIX
        and f.status in (STATUS_INGESTED, STATUS_SKIPPED)
    ]
    docs: dict[str, SourcePdf | None] = {f.doc_id: sources.get(f.doc_id) for f in files}
    images = sync_page_images(
        chunk_db_path,
        docs,
        image_dir=image_dir,
        dpi=dpi,
        max_side=max_side,
        dry_run=report.dry_run,
    )
    if not report.dry_run:
        sync_page_tags(chunk_db_path, {f.doc_id: (Path(f.path), docs[f.doc_id]) for f in files})
    return images


def sync_page_tags(
    chunk_db_path: str | Path, docs: Mapping[str, tuple[Path, SourcePdf | None]]
) -> None:
    """关联了 PDF 的 ``.md`` 逐页算原始度量写进 ``page_tag``（签名不变即跳过）；没关联的撤下旧标签。"""
    written = unchanged = cleared = n_pages = 0
    store = PageTagStore(chunk_db_path)
    try:
        for doc_id, (md_path, source) in docs.items():
            if source is None:
                cleared += 1 if store.clear_doc(doc_id) else 0
                continue
            md_sha256 = hashlib.sha256(md_path.read_bytes()).hexdigest()
            if store.doc_signature(doc_id) == tag_signature(md_sha256):
                unchanged += 1
                continue
            n_pages += store.replace_doc(
                doc_id, compute_markdown_tags(md_path), md_sha256=md_sha256
            )
            written += 1
    finally:
        store.close()
    if written or unchanged or cleared:
        emit_trace(
            None,
            op="narrative.page_tag_index",
            written=written,
            unchanged=unchanged,
            cleared=cleared,
            n_pages=n_pages,
        )


def page_image_report_to_dict(report: PageImageReport) -> dict[str, Any]:
    """纯 JSON（只有 doc_id / 状态 / sha256 / 计数，没有路径）。"""
    return {"counts": report.counts(), "docs": [asdict(d) for d in report.docs]}
