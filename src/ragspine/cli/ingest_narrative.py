"""叙事语料批量入库 CLI：文件夹 / 文件 -> 抽取 -> 切块 -> 块库（幂等、可 dry-run）。

用法（从项目根目录）：
    python scripts/ingest_narrative.py <文件夹或文件...>
    python scripts/ingest_narrative.py docs_in/ --db data/fact_metric.db \\
        --meta meta.json --dry-run

--meta 为 per-doc 元数据 JSON：{文件名: {topic/entity/geography/period/
language/sensitivity/title/valid_as_of}}；缺省时仅从文件名启发式提取 period，
topic/entity 绝不猜测。退出码：有 failed 文件为 1，source PDF 关联校验失败为 2，否则 0。

--source-pdf 给单个 DI markdown 关联原 PDF（也可用 sidecar ``<stem>.meta.json`` 的 ``source_pdf`` 字段），
入库时把每页渲染成 PNG（``--page-image-dpi`` / ``--page-image-max-side``，默认 144 / 1568），存到
``--page-image-dir``（默认块库旁 ``page_images/``）。
"""

import argparse
import json
import sys

from ragspine.common.core import DEFAULT_FACT_DB
from ragspine.ingestion.narrative.narrative_ingest import (
    STATUS_FAILED,
    _resolve_inputs,
    ingest_narrative,
)
from ragspine.ingestion.page_images.index import sync_ingested_page_images
from ragspine.ingestion.page_images.render import (
    DEFAULT_PAGE_IMAGE_DPI,
    DEFAULT_PAGE_IMAGE_MAX_SIDE,
)
from ragspine.ingestion.page_images.source_pdf import SourcePdfError, prepare_source_pdfs
from ragspine.retrieval.chunking.chunk_store import ChunkStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAGSpine 叙事语料批量入库")
    parser.add_argument("inputs", nargs="+", help="文件夹或 pptx/pdf 文件路径")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_FACT_DB),
        help=f"块库 sqlite 路径（默认 {DEFAULT_FACT_DB}，narrative_chunk + narrative_doc 表）",
    )
    parser.add_argument(
        "--meta",
        default=None,
        help="per-doc 元数据 JSON 文件：{文件名: {topic/entity/...}}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告将要做什么，不落库",
    )
    parser.add_argument(
        "--segment-chunking",
        action="store_true",
        help="按 segment 分别切块，chunk locator 带段定位（如 page=N）；.md 恒按段切块",
    )
    parser.add_argument(
        "--source-pdf",
        default=None,
        help="给单个 DI markdown 关联原 PDF（页数须一致），入库时渲染页图；缺省读 <stem>.meta.json 的 source_pdf",
    )
    parser.add_argument("--page-image-dpi", type=int, default=DEFAULT_PAGE_IMAGE_DPI)
    parser.add_argument("--page-image-max-side", type=int, default=DEFAULT_PAGE_IMAGE_MAX_SIDE)
    parser.add_argument(
        "--page-image-dir", default=None, help="页图目录（默认块库旁 page_images/）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    meta_by_doc = None
    if args.meta:
        with open(args.meta, encoding="utf-8") as f:
            meta_by_doc = json.load(f)

    try:
        pdf_sources = prepare_source_pdfs(_resolve_inputs(args.inputs), args.source_pdf)
    except SourcePdfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    store = ChunkStore(args.db)
    store.init_schema()
    try:
        report = ingest_narrative(
            args.inputs,
            store,
            meta_by_doc=meta_by_doc,
            dry_run=args.dry_run,
            segment_chunking=args.segment_chunking,
        )
    finally:
        store.close()
    images = sync_ingested_page_images(
        report,
        pdf_sources,
        args.db,
        image_dir=args.page_image_dir,
        dpi=args.page_image_dpi,
        max_side=args.page_image_max_side,
    )

    prefix = "[dry-run] " if report.dry_run else ""
    for fr in report.files:
        detail = f"chunks={fr.n_chunks}"
        if fr.n_skipped_pages:
            detail += f", 跳过扫描页={fr.n_skipped_pages}"
        if fr.error:
            detail += f", error={fr.error}"
        print(f"{prefix}{fr.doc_id}: {fr.status} ({detail})")
        for w in fr.warnings:
            print(f"    警告: {w}")

    counts = report.counts()
    total_chunks = sum(fr.n_chunks for fr in report.files)
    total_skipped_pages = sum(fr.n_skipped_pages for fr in report.files)
    print(
        f"{prefix}汇总: ingested={counts['ingested']} skipped={counts['skipped']} "
        f"no_text={counts['no_text']} failed={counts['failed']} "
        f"(chunks={total_chunks}, 跳过扫描页={total_skipped_pages})"
    )
    for doc in images.docs:
        print(
            f"{prefix}页图 {doc.doc_id}: {doc.status} "
            f"(pages={doc.n_pages}, images={doc.n_images}, withheld={doc.n_withheld})"
        )
    return 1 if counts[STATUS_FAILED] else 0


if __name__ == "__main__":
    raise SystemExit(main())
