"""结构化入库生产 CLI：单个 xlsx/pptx/pdf -> fact_store（确定性、可 dry-run）。

数字通路的真实生产调用方：装配 store / registry / queue（init_schema）后调
ingest_file，把抽取出的事实带血缘与本批生效日（--valid-as-of）写入 fact_metric，
并打印 IngestReport 摘要（n_grids / n_facts_* / n_enqueued_review / warnings /
status）。可选 --manifest-db + --batch-id 把本文件登记进批次台账。

用法（从项目根目录）：
    python scripts/ingest.py <file> --db data/fact_metric.db \\
        --mapping-db /tmp/mapping.db --queue-db /tmp/queue.db \\
        --valid-as-of 2025-12-31
    python scripts/ingest.py <file> --db /tmp/x.db --mapping-db /tmp/m.db \\
        --queue-db /tmp/q.db --dry-run

退出码：status='failed' 为 1，否则 0。
"""

import argparse
from pathlib import Path

from ragspine.common.core import (
    DEFAULT_FACT_DB,
    DEFAULT_MAPPING_DB,
    DEFAULT_REVIEW_QUEUE_DB,
)
from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_file
from ragspine.ingestion.structured.ingestion_manifest import ManifestStore
from ragspine.storage.fact_store import SqliteFactStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAGSpine 结构化入库（xlsx/pptx/pdf）")
    parser.add_argument("file", help="待入库的 xlsx/pptx/pdf 文件路径")
    parser.add_argument(
        "--db", default=str(DEFAULT_FACT_DB),
        help=f"fact_store sqlite 路径（默认 {DEFAULT_FACT_DB}，fact_metric 表）",
    )
    parser.add_argument(
        "--mapping-db", default=str(DEFAULT_MAPPING_DB),
        help=f"MappingRegistry sqlite 路径（默认 {DEFAULT_MAPPING_DB}，颜色映射注册表）",
    )
    parser.add_argument(
        "--queue-db", default=str(DEFAULT_REVIEW_QUEUE_DB),
        help=f"ReviewQueue sqlite 路径（默认 {DEFAULT_REVIEW_QUEUE_DB}，复核队列）",
    )
    parser.add_argument(
        "--manifest-db", default=None,
        help="可选 ManifestStore sqlite 路径：传入则把本文件登记进批次台账",
    )
    parser.add_argument(
        "--batch-id", default=None,
        help="可选批次 id（与 --manifest-db 配合，未传则自动分配）",
    )
    parser.add_argument(
        "--valid-as-of", default=None,
        help="本批事实的「截至 / 生效」业务日期（ISO，如 2025-12-31），注入每条 fact",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只跑抽取并产报告，绝不写库 / 队列",
    )
    return parser


def _ensure_parent(db_path: str) -> None:
    """库文件父目录不存在时建出（--db 指向尚不存在的嵌套路径时开箱即用）。"""
    parent = Path(db_path).parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)


def _print_report(report: IngestReport) -> None:
    """打印 IngestReport 摘要（终端一眼看到本批结果）。"""
    prefix = "[dry-run] " if report.dry_run else ""
    print(
        f"{prefix}{report.source_doc_id}: status={report.status} "
        f"n_grids={report.n_grids} n_facts_extracted={report.n_facts_extracted} "
        f"n_facts_ingested={report.n_facts_ingested} "
        f"n_tags_applied={report.n_tags_applied} "
        f"n_enqueued_review={report.n_enqueued_review}"
    )
    if report.error:
        print(f"    error: {report.error}")
    for w in report.warnings:
        print(f"    警告: {w}")


def run(args: argparse.Namespace) -> int:
    """Execute ingestion from an already-parsed shared CLI namespace."""
    for db_path in (args.db, args.mapping_db, args.queue_db):
        _ensure_parent(db_path)

    store = SqliteFactStore(args.db)
    registry = MappingRegistry(args.mapping_db)
    queue = ReviewQueue(args.queue_db)
    store.init_schema()
    registry.init_schema()
    queue.init_schema()

    manifest = None
    batch_id = args.batch_id
    if args.manifest_db:
        _ensure_parent(args.manifest_db)
        manifest = ManifestStore(args.manifest_db)
        manifest.init_schema()
        batch_id = manifest.open_batch(batch_id)

    try:
        report = ingest_file(
            args.file, store, registry, queue,
            dry_run=args.dry_run,
            manifest=manifest,
            batch_id=batch_id,
            valid_as_of=args.valid_as_of,
        )
        if manifest is not None:
            status = "failed" if report.status == "failed" else "done"
            manifest.close_batch(batch_id, status=status)
    finally:
        store.close()
        registry.close()
        queue.close()
        if manifest is not None:
            manifest.close()

    _print_report(report)
    return 1 if report.status == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    return run(_build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
