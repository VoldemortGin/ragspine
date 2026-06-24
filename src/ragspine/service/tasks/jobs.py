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
from typing import Any, cast

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
    build_provider,
    provider_config_dict,
    validate_ingest_path,
)
from ragspine.service.tasks.task_queue import JobError
from ragspine.storage.fact_store import FactStore

STRUCTURED_INGEST_JOB = "ragspine.service.tasks.jobs.run_structured_ingest_job"
NARRATIVE_INGEST_JOB = "ragspine.service.tasks.jobs.run_narrative_ingest_job"
DIFY_WORKFLOW_JOB = "ragspine.service.tasks.jobs.run_dify_workflow_job"

_STRUCTURED_SUFFIXES = (".xlsx", ".xlsm", ".pptx", ".pdf")
_NARRATIVE_SUFFIXES = (".pptx", ".pdf")


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ingest_report_to_dict(report: IngestReport) -> dict[str, Any]:
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


def narrative_report_to_dict(report: NarrativeIngestReport) -> dict[str, Any]:
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


def run_structured_ingest_job(payload: dict[str, Any]) -> dict[str, Any]:
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
            # batch_id was reassigned by manifest.open_batch (-> str) in the same guard.
            manifest.close_batch(cast(str, batch_id), status=status)
        return ingest_report_to_dict(report)
    finally:
        store.close()
        registry.close()
        queue.close()
        if manifest is not None:
            manifest.close()


def run_narrative_ingest_job(payload: dict[str, Any]) -> dict[str, Any]:
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


def run_dify_workflow_job(payload: dict[str, Any]) -> dict[str, Any]:
    """worker 端 Dify 工作流执行 job：自建 provider（不传连接对象），受限执行，返回 JSON 结果。

    payload（纯可序列化）：已编译的 source/warnings/imports（入队方编译并已过 L0 闸，worker
    防御式再过一遍 L0 闸，不信任入队方）+ inputs + provider 配置（worker 用 build_provider 自建，
    绝不接受 provider_expr / provider 实例）+ timeout_s / isolation。返回 {"result": {...}}。
    出错抛 JobError（stage=validation L0 闸不过 / execution 执行失败），由队列归一进 JobStatus.error。
    """
    # 延迟 import：dify runner / 编译器经 [service]+[dify] extra，worker 才需要。
    from ragspine.dify.codegen.emitter import GeneratedCode
    from ragspine.service.dify.runner import (
        DifyRunError,
        DifyTimeoutError,
        run_workflow_isolated,
    )
    from ragspine.service.dify.safety import DifyUnsafeError, assert_runnable

    code = GeneratedCode(
        source=payload["source"],
        entrypoint=payload.get("entrypoint", "run_workflow"),
        imports=tuple(payload.get("imports", ())),
        warnings=tuple(payload.get("warnings", ())),
    )
    # 防御式 L0 闸：worker 不信任入队方，执行前自己再过一遍静态闸。
    try:
        assert_runnable(code)
    except DifyUnsafeError as exc:
        raise JobError(str(exc), stage="validation", retryable=False) from exc

    config = ServiceConfig(
        db_path=payload.get("db_path", ":memory:"),
        provider_type=payload.get("provider_type", "mock"),
        model=payload.get("model", ServiceConfig.model),
        base_url=payload.get("base_url"),
        reference_date=payload.get("reference_date"),
        tokens_per_minute=payload.get("tokens_per_minute", 0),
    )
    provider = build_provider(config)
    try:
        result = run_workflow_isolated(
            code, payload.get("inputs", {}), provider,
            timeout_s=payload.get("timeout_s", 10.0),
            isolation=payload.get("isolation", "inprocess"),
            provider_config=provider_config_dict(config),
        )
    except (DifyRunError, DifyTimeoutError) as exc:
        raise JobError(str(exc), stage="execution", retryable=False) from exc
    return {"result": result, "warnings": list(code.warnings)}
