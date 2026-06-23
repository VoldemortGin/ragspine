"""叙事语料批量入库 CLI：文件夹 / 文件 -> 抽取 -> 切块 -> 块库（幂等、可 dry-run）。

用法（从项目根目录）：
    python scripts/ingest_narrative.py <文件夹或文件...>
    python scripts/ingest_narrative.py docs_in/ --db data/fact_metric.db \\
        --meta meta.json --dry-run

--meta 为 per-doc 元数据 JSON：{文件名: {topic/entity/geography/period/
language/sensitivity/title/valid_as_of}}；缺省时仅从文件名启发式提取 period，
topic/entity 绝不猜测。退出码：有 failed 文件为 1，否则 0。
"""

import argparse
import json

from ragspine.common.core import DEFAULT_FACT_DB
from ragspine.ingestion.narrative.narrative_ingest import STATUS_FAILED, ingest_narrative
from ragspine.retrieval.chunking.chunk_store import ChunkStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAGSpine 叙事语料批量入库")
    parser.add_argument("inputs", nargs="+", help="文件夹或 pptx/pdf 文件路径")
    parser.add_argument(
        "--db", default=str(DEFAULT_FACT_DB),
        help=f"块库 sqlite 路径（默认 {DEFAULT_FACT_DB}，narrative_chunk + narrative_doc 表）",
    )
    parser.add_argument(
        "--meta", default=None,
        help="per-doc 元数据 JSON 文件：{文件名: {topic/entity/...}}",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只报告将要做什么，不落库",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    meta_by_doc = None
    if args.meta:
        with open(args.meta, encoding="utf-8") as f:
            meta_by_doc = json.load(f)

    store = ChunkStore(args.db)
    store.init_schema()
    try:
        report = ingest_narrative(
            args.inputs, store, meta_by_doc=meta_by_doc, dry_run=args.dry_run,
        )
    finally:
        store.close()

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
    return 1 if counts[STATUS_FAILED] else 0


if __name__ == "__main__":
    raise SystemExit(main())
