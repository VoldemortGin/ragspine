"""HTTP 路由：薄适配层。只做 schema/DI/资源装配/FAQ 短路/错误整形/trace。

不重写 Agent/retrieval/ingestion 逻辑——一律调用既有 workflow。
"""

from datetime import date
from typing import Annotated, Any

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
    DifyAnalyzeRequest,
    DifyAnalyzeResponse,
    DifyCompileRequest,
    DifyCompileResponse,
    DifyRunRequest,
    DifyRunResponse,
    DifySuggestionInfo,
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
DIFY_WORKFLOW_JOB = "ragspine.service.tasks.jobs.run_dify_workflow_job"

# ingestion 路径允许后缀
_STRUCTURED_SUFFIXES = (".xlsx", ".xlsm", ".pptx", ".pdf")
_NARRATIVE_SUFFIXES = (".pptx", ".pdf")

router = APIRouter()

# DI 别名（Annotated 形式，避免在参数默认值里直接调用 Depends）。
ConfigDep = Annotated[ServiceConfig, Depends(get_config)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]
FAQCacheDep = Annotated[FAQCache, Depends(get_faq_cache)]
QueueDep = Annotated[TaskQueue, Depends(get_queue)]


def _error_response(status_code: int, *, type_: str, message: str,
                    request_id: str | None = None) -> JSONResponse:
    payload = ErrorResponse(
        error={"type": type_, "message": message, "request_id": request_id}
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _tool_status_summary(tool_results: list[dict[str, Any]]) -> dict[str, int]:
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


def _answer_kind(result: AgentResult, summary: dict[str, int]) -> str:
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
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(
    config: ConfigDep,
    queue: QueueDep,
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
    config: ConfigDep,
    provider: ProviderDep,
    faq_cache: FAQCacheDep,
) -> AskResponse | JSONResponse:
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
            sources=[SourceInfo.model_validate(s) for s in result.sources],
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
    config: ConfigDep,
    queue: QueueDep,
) -> JobSubmitResponse | JSONResponse:
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
    config: ConfigDep,
    queue: QueueDep,
) -> JobSubmitResponse | JSONResponse:
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
    queue: QueueDep,
) -> JobStatusResponse | JSONResponse:
    st = queue.get(job_id)
    if st is None:
        return _error_response(404, type_="JobNotFound", message="job not found")
    return JobStatusResponse(
        id=st.id, status=st.status, result=st.result, error=st.error
    )


# ---------------------------------------------------------------------------
# Dify 工作流编译 / 运行
#
# 三个端点信任级别递增：analyze（只建议，安全）→ compile（代码字符串，安全）→
# run（编译+受限执行，信任边界，默认关闭）。dify 编译器经 [dify] extra 延迟 import，
# DifyCompileError（code dify.*）整形为 400。provider 由服务端 env 决定，客户端不可注入。
# ---------------------------------------------------------------------------
def _dify_suggestion_info(s: Any) -> DifySuggestionInfo:
    """内部 Suggestion dataclass -> 对外 DifySuggestionInfo（enum 取 .value）。"""
    return DifySuggestionInfo(
        rule_id=s.rule_id,
        severity=s.severity.value,
        category=s.category.value,
        title=s.title,
        detail=s.detail,
        node_ids=list(s.node_ids),
    )


@router.post("/v1/dify/analyze", response_model=None)
def dify_analyze(req: DifyAnalyzeRequest) -> DifyAnalyzeResponse | JSONResponse:
    request_id = new_request_id()
    # 延迟 import：[dify] extra 未装时只这条链报错，不影响其余服务。
    from ragspine.dify.api import analyze as analyze_dify
    from ragspine.dify.errors import DifyCompileError

    try:
        suggestions = analyze_dify(req.yaml)
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    emit_trace(request_id=request_id, op="dify.analyze", n_suggestions=len(suggestions))
    return DifyAnalyzeResponse(
        request_id=request_id,
        suggestions=[_dify_suggestion_info(s) for s in suggestions],
    )


@router.post("/v1/dify/compile", response_model=None)
def dify_compile(req: DifyCompileRequest) -> DifyCompileResponse | JSONResponse:
    request_id = new_request_id()
    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError

    try:
        # provider_expr 固定为离线默认；客户端不可注入（防代码注入），运行期再由
        # run_workflow(provider=...) 用服务端 provider 覆盖。
        compiled = compile_dify_yaml(
            req.yaml,
            target=req.target,
            fold_answer_question=req.fold_answer_question,
        )
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    code = compiled.code
    emit_trace(
        request_id=request_id, op="dify.compile", target=req.target,
        n_warnings=len(code.warnings), n_suggestions=len(compiled.suggestions),
    )
    return DifyCompileResponse(
        request_id=request_id,
        code=code.source,
        entrypoint=code.entrypoint,
        imports=list(code.imports),
        warnings=list(code.warnings),
        suggestions=[_dify_suggestion_info(s) for s in compiled.suggestions],
    )


@router.post("/v1/dify/run", response_model=None)
def dify_run(
    req: DifyRunRequest,
    config: ConfigDep,
    provider: ProviderDep,
) -> DifyRunResponse | JSONResponse:
    """编译 + 受限执行（信任边界）。默认关（dify_run_enabled=False）；provider 由服务端注入。

    错误分级整形：未开启 -> 403；编译失败（DifyCompileError，code dify.*）-> 400；
    L0 静态闸不过（DifyUnsafeError，如不支持节点 / 越权 import）-> 422；
    执行失败 / 超时（DifyRunError / DifyTimeoutError）-> 400。
    """
    request_id = new_request_id()

    # 信任边界开关：默认关，env 显式开（RAGSPINE_DIFY_RUN_ENABLED=true）才放行。
    if not config.dify_run_enabled:
        return _error_response(
            403, type_="dify.run_disabled",
            message="dify 工作流执行未开启（设 RAGSPINE_DIFY_RUN_ENABLED=true 开启）",
            request_id=request_id,
        )

    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.runner import (
        DifyRunError,
        DifyTimeoutError,
        run_workflow_isolated,
    )
    from ragspine.service.dify.safety import DifyUnsafeError

    try:
        compiled = compile_dify_yaml(
            req.yaml, fold_answer_question=req.fold_answer_question
        )
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    code = compiled.code
    try:
        result = run_workflow_isolated(
            code, req.inputs, provider,
            timeout_s=config.dify_run_timeout_s,
            isolation=config.dify_run_isolation,
        )
    except DifyUnsafeError as exc:
        # L0 静态闸：含 NotImplementedError 骨架 / 不支持节点 / 越权 import -> 422（未执行）。
        return _error_response(422, type_=exc.code, message=str(exc), request_id=request_id)
    except (DifyRunError, DifyTimeoutError) as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    emit_trace(
        request_id=request_id, op="dify.run",
        isolation=config.dify_run_isolation, n_result_keys=len(result),
    )
    return DifyRunResponse(
        request_id=request_id, result=result, warnings=list(code.warnings)
    )


@router.post("/v1/dify/run/jobs", response_model=None)
def dify_run_async(
    req: DifyRunRequest,
    config: ConfigDep,
    queue: QueueDep,
) -> JobSubmitResponse | JSONResponse:
    """异步执行：编译 + L0 闸同步先行（快速失败），再入队执行；状态经 GET /v1/jobs/{id} 取。

    同步段：未开启 -> 403；编译失败 -> 400；L0 静态闸不过 -> 422（均在入队前）。
    入队 payload 纯可序列化（已编译 source/warnings + inputs + provider 配置），worker 自建
    provider 并防御式再过一遍 L0 闸。provider 配置由服务端 config 决定，客户端不可注入。
    """
    request_id = new_request_id()
    if not config.dify_run_enabled:
        return _error_response(
            403, type_="dify.run_disabled",
            message="dify 工作流执行未开启（设 RAGSPINE_DIFY_RUN_ENABLED=true 开启）",
            request_id=request_id,
        )

    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.safety import DifyUnsafeError, assert_runnable

    try:
        compiled = compile_dify_yaml(
            req.yaml, fold_answer_question=req.fold_answer_question
        )
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    code = compiled.code
    try:
        assert_runnable(code)  # L0 闸同步先行，快速失败（worker 仍会防御式再过一遍）。
    except DifyUnsafeError as exc:
        return _error_response(422, type_=exc.code, message=str(exc), request_id=request_id)

    payload = {
        "source": code.source,
        "entrypoint": code.entrypoint,
        "imports": list(code.imports),
        "warnings": list(code.warnings),
        "inputs": req.inputs,
        "timeout_s": config.dify_run_timeout_s,
        "isolation": config.dify_run_isolation,
        # provider 配置（worker 用 build_provider 自建；绝不传 provider 实例 / provider_expr）。
        "provider_type": config.provider_type,
        "model": config.model,
        "base_url": config.base_url,
        "reference_date": config.reference_date,
        "tokens_per_minute": config.tokens_per_minute,
    }
    job_id = queue.enqueue(DIFY_WORKFLOW_JOB, payload)
    return JobSubmitResponse(job_id=job_id)
