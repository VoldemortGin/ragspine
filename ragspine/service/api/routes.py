"""HTTP 路由：薄适配层。只做 schema/DI/资源装配/FAQ 短路/错误整形/trace。

不重写 Agent/retrieval/ingestion 逻辑——一律调用既有 workflow。
"""

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.intent import (
    CLARIFY_ASK_FIRST,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_COMPOSITE,
    ROUTE_STRUCTURED,
)
from ragspine.agent.llm_provider import LLMProvider
from ragspine.common.observability import emit_trace, new_request_id
from ragspine.service.api.dependencies import (
    get_config,
    get_faq_cache,
    get_provider,
    get_queue,
)
from ragspine.service.api.schemas import (
    AskRequest,
    AskResponse,
    CacheInfo,
    ClarificationInfo,
    ErrorResponse,
    IngestNarrativeJobRequest,
    IngestStructuredJobRequest,
    JobStatusResponse,
    JobSubmitResponse,
    SourceInfo,
)
from ragspine.service.config import (
    PathNotAllowedError,
    ServiceConfig,
    open_fact_store,
    open_narrative_retriever,
    validate_ingest_path,
)
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import TaskQueue

# 与 worker 侧 jobs 模块约定的 func_path（此处只用字面量，不 import jobs）。
STRUCTURED_INGEST_JOB = "ragspine.service.tasks.jobs.run_structured_ingest_job"
NARRATIVE_INGEST_JOB = "ragspine.service.tasks.jobs.run_narrative_ingest_job"

# ingestion 路径允许后缀
_STRUCTURED_SUFFIXES = (".xlsx", ".xlsm", ".pptx", ".pdf")
_NARRATIVE_SUFFIXES = (".pptx", ".pdf")

router = APIRouter()


def _error_response(status_code: int, *, type_: str, message: str,
                    request_id: str | None = None) -> JSONResponse:
    payload = ErrorResponse(
        error={"type": type_, "message": message, "request_id": request_id}
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _tool_status_summary(tool_results: list[dict]) -> dict:
    counts = {"found": 0, "not_found": 0, "unrecognized": 0}
    for r in tool_results:
        status = r.get("status")
        if status == "found":
            counts["found"] += 1
        elif status == "not_found":
            counts["not_found"] += 1
        elif status == "unrecognized_param":
            counts["unrecognized"] += 1
    return counts


def _answer_kind(result: AgentResult, summary: dict) -> str:
    clar = result.clarification
    if clar is not None and clar.mode == CLARIFY_ASK_FIRST:
        return "clarification"
    if clar is not None and clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
        return "refusal"
    if (
        result.route in (ROUTE_STRUCTURED, ROUTE_COMPOSITE)
        and summary["found"] == 0
        and (summary["not_found"] + summary["unrecognized"]) > 0
    ):
        return "refusal"
    return "normal"


def _clarification_info(result: AgentResult) -> ClarificationInfo | None:
    clar = result.clarification
    if clar is None:
        return None
    return ClarificationInfo(
        mode=clar.mode,
        question=getattr(clar, "question", None),
        narrowing_options=list(getattr(clar, "narrowing_options", []) or []),
        assumption_note=getattr(clar, "assumption_note", None),
    )


def _ref_date(req_ref: str | None, config: ServiceConfig) -> date | None:
    raw = req_ref or config.reference_date
    return date.fromisoformat(raw) if raw else None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(
    config: ServiceConfig = Depends(get_config),
    queue: TaskQueue = Depends(get_queue),
) -> JSONResponse:
    checks: dict[str, bool] = {}

    try:
        with open_fact_store(config):
            checks["fact_db"] = True
    except Exception:
        checks["fact_db"] = False

    ping = getattr(queue, "ping", None)
    if callable(ping):
        try:
            checks["queue"] = bool(ping())
        except Exception:
            checks["queue"] = False

    ready = all(checks.values())
    status = "ready" if ready else "degraded"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": status, "checks": checks},
    )


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------
@router.post("/v1/ask", response_model=None)
def ask(
    req: AskRequest,
    config: ServiceConfig = Depends(get_config),
    provider: LLMProvider = Depends(get_provider),
    faq_cache: FAQCache = Depends(get_faq_cache),
):
    request_id = new_request_id()
    try:
        ref = _ref_date(req.reference_date, config)

        # 1) FAQ 短路（命中即返回，绝不触达 provider/fact store/retriever）
        hit = faq_cache.lookup(req.question, reference_date=ref)
        if hit is not None:
            emit_trace(
                request_id=request_id, cache_hit=True,
                faq_id=hit.item_id, faq_version=hit.version,
            )
            return AskResponse(
                request_id=request_id,
                answer=hit.answer,
                route="faq",
                answer_kind="normal",
                clarification=None,
                sources=[SourceInfo(doc=hit.source)] if hit.source else [],
                tool_status_summary={"found": 0, "not_found": 0, "unrecognized": 0},
                cache=CacheInfo(
                    hit=True, type=hit.cache_type, faq_id=hit.item_id,
                    version=hit.version, source=hit.source,
                ),
            )

        # 2) miss -> 正常 workflow（资源每请求各自开关）
        with open_fact_store(config) as store, \
                open_narrative_retriever(config, provider) as retriever:
            result = answer_question(
                req.question, store, provider,
                reference_date=ref, narrative_retriever=retriever,
            )

        summary = _tool_status_summary(result.tool_results)
        answer_kind = _answer_kind(result, summary)
        emit_trace(
            request_id=request_id, cache_hit=False, route=result.route,
            answer_kind=answer_kind, found=summary["found"],
            not_found=summary["not_found"], unrecognized=summary["unrecognized"],
        )
        return AskResponse(
            request_id=request_id,
            answer=result.answer,
            route=result.route,
            answer_kind=answer_kind,
            clarification=_clarification_info(result),
            sources=[SourceInfo(**s) for s in result.sources],
            tool_status_summary=summary,
            cache=CacheInfo(hit=False),
        )
    except Exception:  # 防御性兜底：绝不泄露 traceback
        return _error_response(
            500, type_="InternalError", message="internal error",
            request_id=request_id,
        )


# ---------------------------------------------------------------------------
# ingestion jobs
# ---------------------------------------------------------------------------
@router.post("/v1/ingest/structured/jobs", response_model=None)
def submit_structured_job(
    req: IngestStructuredJobRequest,
    config: ServiceConfig = Depends(get_config),
    queue: TaskQueue = Depends(get_queue),
):
    try:
        resolved = validate_ingest_path(
            req.file, config, suffixes=_STRUCTURED_SUFFIXES
        )
    except PathNotAllowedError as exc:
        return _error_response(400, type_="PathNotAllowedError", message=str(exc))

    payload = {
        "file": str(resolved),
        "db_path": config.db_path,
        "mapping_db_path": config.mapping_db_path,
        "queue_db_path": config.queue_db_path,
        "manifest_db_path": config.manifest_db_path,
        "batch_id": req.batch_id,
        "dry_run": req.dry_run,
        "valid_as_of": req.valid_as_of,
        "allowed_upload_root": config.allowed_upload_root,
    }
    job_id = queue.enqueue(STRUCTURED_INGEST_JOB, payload, job_id=req.job_id)
    return JobSubmitResponse(job_id=job_id)


@router.post("/v1/ingest/narrative/jobs", response_model=None)
def submit_narrative_job(
    req: IngestNarrativeJobRequest,
    config: ServiceConfig = Depends(get_config),
    queue: TaskQueue = Depends(get_queue),
):
    raw_inputs = [req.inputs] if isinstance(req.inputs, str) else list(req.inputs)
    resolved_inputs: list[str] = []
    try:
        for item in raw_inputs:
            resolved = validate_ingest_path(
                item, config, suffixes=_NARRATIVE_SUFFIXES
            )
            resolved_inputs.append(str(resolved))
    except PathNotAllowedError as exc:
        return _error_response(400, type_="PathNotAllowedError", message=str(exc))

    payload = {
        "inputs": resolved_inputs,
        "chunk_db_path": config.chunk_db_path,
        "meta_by_doc": req.meta_by_doc,
        "dry_run": req.dry_run,
        "allowed_upload_root": config.allowed_upload_root,
    }
    job_id = queue.enqueue(NARRATIVE_INGEST_JOB, payload, job_id=req.job_id)
    return JobSubmitResponse(job_id=job_id)


@router.get("/v1/jobs/{job_id}", response_model=None)
def get_job(
    job_id: str,
    queue: TaskQueue = Depends(get_queue),
):
    st = queue.get(job_id)
    if st is None:
        return _error_response(404, type_="JobNotFound", message="job not found")
    return JobStatusResponse(
        id=st.id, status=st.status, result=st.result, error=st.error
    )
