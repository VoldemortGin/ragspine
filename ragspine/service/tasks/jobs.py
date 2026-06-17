"""worker 端 durable ingestion job 函数。

设计要点：
- worker 自有资源：每个 job 用 payload 里的路径自行打开 store / registry / queue /
  manifest / chunk store，跑完在 finally 里全部关闭。不复用调用方的 sqlite 连接。
- payload 纯可序列化 dict 进，纯 JSON report 出——report 只含计数 / 状态 / 告警，
  绝不返回原始 fact 数值、chunk 正文或文件内容。
- 防御式路径再校验：worker 不信任入队方，落地前再走一遍 allowed_upload_root +
  后缀白名单，越界 / 不支持后缀抛 JobError(stage="validation")。
- 这两个函数即 RQ/FakeQueue 通过 func_path 解析并执行的目标。
"""

from pathlib import Path

from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.ingestion.narrative.narrative_ingest import (
    NarrativeIngestReport,
    ingest_narrative,
)
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_file
from ragspine.ingestion.structured.ingestion_manifest import ManifestStore
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.service.config import (
    PathNotAllowedError,
    ServiceConfig,
    validate_ingest_path,
)
from ragspine.service.tasks.task_queue import JobError
from ragspine.storage.fact_store import FactStore

STRUCTURED_INGEST_JOB = "ragspine.service.tasks.jobs.run_structured_ingest_job"
NARRATIVE_INGEST_JOB = "ragspine.service.tasks.jobs.run_narrative_ingest_job"

_STRUCTURED_SUFFIXES = (".xlsx", ".xlsm", ".pptx", ".pdf")
_NARRATIVE_SUFFIXES = (".pptx", ".pdf")


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ingest_report_to_dict(report: IngestReport) -> dict:
    """IngestReport -> 纯 JSON dict（只含计数 / 状态 / 告警，无原始事实数值）。"""
    return {
        "source_path": report.source_path,
        "source_doc_id": report.source_doc_id,
        "file_hash": report.file_hash,
        "dry_run": report.dry_run,
        "n_grids": report.n_grids,
        "n_facts_extracted": report.n_facts_extracted,
        "n_facts_ingested": report.n_facts_ingested,
        "n_tags_applied": report.n_tags_applied,
        "n_enqueued_review": report.n_enqueued_review,
        "warnings": list(report.warnings),
        "status": report.status,
        "error": report.error,
    }


def narrative_report_to_dict(report: NarrativeIngestReport) -> dict:
    """NarrativeIngestReport -> 纯 JSON dict（逐文件状态，无 chunk 正文）。"""
    return {
        "dry_run": report.dry_run,
        "counts": report.counts(),
        "files": [
            {
                "path": f.path,
                "doc_id": f.doc_id,
                "status": f.status,
                "n_chunks": f.n_chunks,
                "n_skipped_pages": f.n_skipped_pages,
                "file_hash": f.file_hash,
                "error": f.error,
                "warnings": list(f.warnings),
            }
            for f in report.files
        ],
    }


def run_structured_ingest_job(payload: dict) -> dict:
    """结构化 ingestion worker job：自开自闭 store，返回 JSON report。"""
    config = ServiceConfig(
        db_path=payload["db_path"],
        allowed_upload_root=payload.get("allowed_upload_root"),
    )
    try:
        file_path = validate_ingest_path(
            payload["file"], config, suffixes=_STRUCTURED_SUFFIXES
        )
    except PathNotAllowedError as exc:
        raise JobError(str(exc), stage="validation", retryable=False) from exc

    db_path = payload["db_path"]
    mapping_db_path = payload["mapping_db_path"]
    queue_db_path = payload["queue_db_path"]
    manifest_db_path = payload.get("manifest_db_path")

    for p in (db_path, mapping_db_path, queue_db_path):
        _ensure_parent(p)

    store = FactStore(db_path)
    registry = MappingRegistry(mapping_db_path)
    queue = ReviewQueue(queue_db_path)
    store.init_schema()
    registry.init_schema()
    queue.init_schema()

    manifest = None
    batch_id = payload.get("batch_id")
    if manifest_db_path:
        _ensure_parent(manifest_db_path)
        manifest = ManifestStore(manifest_db_path)
        manifest.init_schema()
        batch_id = manifest.open_batch(batch_id)

    try:
        report = ingest_file(
            file_path,
            store,
            registry,
            queue,
            dry_run=payload.get("dry_run", False),
            manifest=manifest,
            batch_id=batch_id,
            valid_as_of=payload.get("valid_as_of"),
        )
        if manifest is not None:
            status = "failed" if report.status == "failed" else "done"
            manifest.close_batch(batch_id, status=status)
        return ingest_report_to_dict(report)
    finally:
        store.close()
        registry.close()
        queue.close()
        if manifest is not None:
            manifest.close()


def run_narrative_ingest_job(payload: dict) -> dict:
    """叙事 ingestion worker job：自开自闭 ChunkStore，返回 JSON report。"""
    inputs = payload["inputs"]
    allowed_upload_root = payload.get("allowed_upload_root")
    if allowed_upload_root is not None:
        config = ServiceConfig(
            db_path=payload["chunk_db_path"],
            allowed_upload_root=allowed_upload_root,
        )
        paths = [inputs] if isinstance(inputs, str) else inputs
        for path in paths:
            try:
                validate_ingest_path(path, config, suffixes=_NARRATIVE_SUFFIXES)
            except PathNotAllowedError as exc:
                raise JobError(str(exc), stage="validation", retryable=False) from exc

    chunk_db_path = payload["chunk_db_path"]
    _ensure_parent(chunk_db_path)

    store = ChunkStore(chunk_db_path)
    store.init_schema()
    try:
        report = ingest_narrative(
            inputs,
            store,
            meta_by_doc=payload.get("meta_by_doc"),
            dry_run=payload.get("dry_run", False),
        )
        return narrative_report_to_dict(report)
    finally:
        store.close()
