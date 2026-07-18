"""HTTP 路由：薄适配层。只做 schema/DI/资源装配/FAQ 短路/错误整形/trace。

不重写 Agent/retrieval/ingestion 逻辑——一律调用既有 workflow。
"""

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.decompose import make_decomposer
from ragspine.agent.intent import (
    CLARIFY_ASK_FIRST,
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_COMPOSITE,
    ROUTE_STRUCTURED,
)
from ragspine.agent.llm_provider import LLMProvider, iter_text_chunks
from ragspine.agent.query_transform import make_adaptive_decomposer
from ragspine.common.observability import emit_trace, new_request_id
from ragspine.pipeline.topology import agent_topology, retriever_topology, service_topology
from ragspine.service.api.dependencies import (
    get_config,
    get_faq_cache,
    get_launch_sessions,
    get_provider,
    get_queue,
    get_workflow_matcher,
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
    LaunchSessionResponse,
    N8nConvertRequest,
    N8nConvertResponse,
    N8nRunRequest,
    N8nRunResponse,
    SourceInfo,
    TopologyEdgeInfo,
    TopologyNodeInfo,
    TopologyResponse,
    WorkflowCompatibilityInfo,
    WorkflowRequirementInfo,
    WorkflowScaffoldRequest,
    WorkflowScaffoldResponse,
    WorkflowSourceMetadata,
    WorkflowTemplateDetailResponse,
    WorkflowTemplateInfo,
    WorkflowTemplateListResponse,
)
from ragspine.service.config import (
    PathNotAllowedError,
    ServiceConfig,
    open_fact_store,
    open_narrative_retriever,
    provider_config_dict,
    validate_ingest_path,
)
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.studio.launch import LaunchSessionRegistry
from ragspine.service.tasks.task_queue import TaskQueue
from ragspine.workflows.matching import LexicalTemplateMatcher, TemplateMatcher

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
WorkflowMatcherDep = Annotated[TemplateMatcher, Depends(get_workflow_matcher)]
LaunchSessionsDep = Annotated[LaunchSessionRegistry, Depends(get_launch_sessions)]

_WORKFLOW_LEXICAL_FALLBACK = LexicalTemplateMatcher()
_WORKFLOW_MATCHER_FALLBACK_WARNING = (
    "Semantic template matching failed; used the offline lexical fallback."
)


def _error_response(
    status_code: int,
    *,
    type_: str,
    message: str,
    request_id: str | None = None,
    node_traces: Any = None,
) -> JSONResponse:
    error: dict[str, Any] = {"type": type_, "message": message, "request_id": request_id}
    if node_traces:
        # dify run 失败时附带节点级 trace（已净化、JSON-safe），供前端可视化失败节点。
        error["node_traces"] = node_traces
    payload = ErrorResponse(error=error)
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
                request_id=request_id,
                cache_hit=True,
                faq_id=hit.item_id,
                faq_version=hit.version,
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
                    hit=True,
                    type=hit.cache_type,
                    faq_id=hit.item_id,
                    version=hit.version,
                    source=hit.source,
                ),
            )

        # 2) miss -> 正常 workflow（资源每请求各自开关）
        with (
            open_fact_store(config) as store,
            open_narrative_retriever(config, provider) as retriever,
        ):
            # 分解器选型：W9 Adaptive-RAG（复杂度路由）优先于 W6a 直接分解——config.adaptive
            # 非 "none" 时按复杂度路由（multi 才 fan-out），否则用 W6a 直接分解。两者默认均 "none"
            # → decomposer=None → answer_question 主流程字节不变。
            if config.adaptive != "none":
                decomposer = make_adaptive_decomposer(config.adaptive, provider=provider)
            else:
                decomposer = make_decomposer(config.query_decompose, provider=provider)
            result = answer_question(
                req.question,
                store,
                provider,
                reference_date=ref,
                narrative_retriever=retriever,
                decomposer=decomposer,
                history=req.history,
            )

        summary = _tool_status_summary(result.tool_results)
        answer_kind = _answer_kind(result, summary)
        emit_trace(
            request_id=request_id,
            cache_hit=False,
            route=result.route,
            answer_kind=answer_kind,
            found=summary["found"],
            not_found=summary["not_found"],
            unrecognized=summary["unrecognized"],
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
            500,
            type_="InternalError",
            message="internal error",
            request_id=request_id,
        )


def _sse_frame(event: dict[str, Any]) -> str:
    """SSE 单帧封装，与 dify_public._sse_iter 同款 `data: {json}\\n\\n` 形状。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/v1/ask/stream", response_model=None)
def ask_stream(
    req: AskRequest,
    config: ConfigDep,
    provider: ProviderDep,
    faq_cache: FAQCacheDep,
) -> StreamingResponse | JSONResponse:
    """SSE 流式 ask：与 /v1/ask 完全同一守卫链，但把已守卫的 answer 分块推送。

    **反编造纪律**：整条守卫链（含 not_found→拒答改写）在流打开之前跑到完成——所有
    provider/store/retriever 访问都在下方 try 块（生成器之外）里；生成器只回放已算好的
    answer 字符串，零模型/存储访问。因此 not-found 流只可能承载拒答文案。守卫前置计算
    任何失败 → 正常 JSON 500（绝不半开流）。
    """
    request_id = new_request_id()
    try:
        ref = _ref_date(req.reference_date, config)

        # 1) FAQ 短路（命中即用缓存答案，绝不触达 provider/fact store/retriever）
        hit = faq_cache.lookup(req.question, reference_date=ref)
        if hit is not None:
            emit_trace(
                request_id=request_id,
                cache_hit=True,
                faq_id=hit.item_id,
                faq_version=hit.version,
            )
            answer = hit.answer
            route = "faq"
            answer_kind = "normal"
            clarification: dict[str, Any] | None = None
            sources = [SourceInfo(doc=hit.source).model_dump()] if hit.source else []
            tool_status_summary = {"found": 0, "not_found": 0, "unrecognized": 0}
            cache = CacheInfo(
                hit=True,
                type=hit.cache_type,
                faq_id=hit.item_id,
                version=hit.version,
                source=hit.source,
            ).model_dump()
        else:
            # 2) miss -> 正常 workflow（资源每请求各自开关；守卫在此跑完）
            with (
                open_fact_store(config) as store,
                open_narrative_retriever(config, provider) as retriever,
            ):
                if config.adaptive != "none":
                    decomposer = make_adaptive_decomposer(config.adaptive, provider=provider)
                else:
                    decomposer = make_decomposer(config.query_decompose, provider=provider)
                result = answer_question(
                    req.question,
                    store,
                    provider,
                    reference_date=ref,
                    narrative_retriever=retriever,
                    decomposer=decomposer,
                    history=req.history,
                )
            summary = _tool_status_summary(result.tool_results)
            answer_kind = _answer_kind(result, summary)
            emit_trace(
                request_id=request_id,
                cache_hit=False,
                route=result.route,
                answer_kind=answer_kind,
                found=summary["found"],
                not_found=summary["not_found"],
                unrecognized=summary["unrecognized"],
            )
            answer = result.answer
            route = result.route
            clar_info = _clarification_info(result)
            clarification = clar_info.model_dump() if clar_info is not None else None
            sources = [SourceInfo.model_validate(s).model_dump() for s in result.sources]
            tool_status_summary = summary
            cache = CacheInfo(hit=False).model_dump()
    except Exception:  # 守卫前置计算失败 -> 正常 JSON 500，绝不半开流
        return _error_response(
            500,
            type_="InternalError",
            message="internal error",
            request_id=request_id,
        )

    def gen() -> Iterator[str]:
        # 仅回放已算好的守卫值，零 provider/store 访问。
        yield _sse_frame({"type": "start", "request_id": request_id})
        for chunk in iter_text_chunks(answer):
            yield _sse_frame({"type": "delta", "text": chunk})
        yield _sse_frame(
            {
                "type": "done",
                "request_id": request_id,
                "route": route,
                "answer_kind": answer_kind,
                "clarification": clarification,
                "sources": sources,
                "tool_status_summary": tool_status_summary,
                "cache": cache,
            }
        )

    return StreamingResponse(gen(), media_type="text/event-stream")


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
        resolved = validate_ingest_path(req.file, config, suffixes=_STRUCTURED_SUFFIXES)
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
            resolved = validate_ingest_path(item, config, suffixes=_NARRATIVE_SUFFIXES)
            resolved_inputs.append(str(resolved))
    except PathNotAllowedError as exc:
        return _error_response(400, type_="PathNotAllowedError", message=str(exc))

    payload = {
        "inputs": resolved_inputs,
        "chunk_db_path": config.chunk_db_path,
        "meta_by_doc": req.meta_by_doc,
        "dry_run": req.dry_run,
        "allowed_upload_root": config.allowed_upload_root,
        "chunker": config.chunker,
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
    return JobStatusResponse(id=st.id, status=st.status, result=st.result, error=st.error)


# ---------------------------------------------------------------------------
# 离线工作流模板 catalog / scaffold
#
# 三个端点都是只读边界：catalog 只来自包内 manifest，scaffold 只调用离线 matcher；
# 不接受 provider/API key/path/URL，也不执行生成的工作流。刻意避开 Dify 兼容 API 已占用的
# /v1/workflows/* 命名空间。
# ---------------------------------------------------------------------------
def _workflow_template_info(template: Any) -> WorkflowTemplateInfo:
    """内部 frozen dataclass -> 不含 YAML 的 HTTP metadata。"""
    return WorkflowTemplateInfo.model_validate(template, from_attributes=True)


def _workflow_compatibility_info(value: Any) -> WorkflowCompatibilityInfo:
    return WorkflowCompatibilityInfo.model_validate(value, from_attributes=True)


def _workflow_requirement_info(value: Any) -> WorkflowRequirementInfo:
    return WorkflowRequirementInfo.model_validate(value, from_attributes=True)


def _workflow_source_metadata(value: Any) -> WorkflowSourceMetadata | None:
    if value is None:
        return None
    return WorkflowSourceMetadata.model_validate(value, from_attributes=True)


def _workflow_preview_json(workflow: dict[str, object]) -> dict[str, object]:
    """Project one canonical workflow into the public graph-only preview contract."""

    from ragspine.workflows.preview import build_workflow_preview

    return build_workflow_preview(workflow).to_dict()


def _if_none_match_matches(value: str | None, current_etag: str) -> bool:
    """Evaluate an If-None-Match field with RFC 9110 weak comparison."""

    if value is None:
        return False
    value = value.strip()
    if value == "*":
        return True
    if not value:
        return False

    current_opaque_tag = current_etag.removeprefix("W/")
    matched = False
    cursor = 0
    while cursor < len(value):
        while cursor < len(value) and value[cursor] in " \t":
            cursor += 1
        if value.startswith("W/", cursor):
            cursor += 2
        if cursor >= len(value) or value[cursor] != '"':
            return False

        tag_start = cursor
        cursor += 1
        while cursor < len(value) and value[cursor] != '"':
            codepoint = ord(value[cursor])
            is_etag_char = (
                codepoint == 0x21 or 0x23 <= codepoint <= 0x7E or 0x80 <= codepoint <= 0xFF
            )
            if not is_etag_char:
                return False
            cursor += 1
        if cursor >= len(value):
            return False
        cursor += 1
        matched = matched or value[tag_start:cursor] == current_opaque_tag

        while cursor < len(value) and value[cursor] in " \t":
            cursor += 1
        if cursor == len(value):
            return matched
        if value[cursor] != ",":
            return False
        cursor += 1
        if cursor == len(value):
            return False

    return False


@router.get("/v1/workflow-templates", response_model=None)
def list_workflow_templates(
    request: Request,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> WorkflowTemplateListResponse | Response:
    """分页列出事实型 metadata；完整 YAML 刻意不进入列表响应。"""
    from ragspine.workflows.catalog import WorkflowCatalog

    request_id = new_request_id()
    templates = WorkflowCatalog.default()._metadata_refs()
    total = len(templates)
    page = templates[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    page_infos = [_workflow_template_info(template) for template in page]

    # request_id is intentionally excluded: this is a weak validator for the
    # catalog representation, while request_id is per-request trace metadata.
    validator_payload = {
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "templates": [info.model_dump(mode="json") for info in page_infos],
    }
    canonical_payload = json.dumps(
        validator_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical_payload)
    etag = f'W/"{digest.hexdigest()}"'
    cache_headers = {
        "Cache-Control": "public, max-age=300, stale-while-revalidate=3600",
        "ETag": etag,
    }
    if_none_match_values = request.headers.getlist("if-none-match")
    if_none_match = ",".join(if_none_match_values) if if_none_match_values else None
    if _if_none_match_matches(if_none_match, etag):
        emit_trace(
            request_id=request_id,
            op="workflow_catalog.list",
            n_templates=0,
            total=total,
            offset=offset,
            limit=limit,
            not_modified=True,
        )
        return Response(status_code=304, headers=cache_headers)

    for name, value in cache_headers.items():
        response.headers[name] = value
    emit_trace(
        request_id=request_id,
        op="workflow_catalog.list",
        n_templates=len(page),
        total=total,
        offset=offset,
        limit=limit,
    )
    return WorkflowTemplateListResponse(
        request_id=request_id,
        total=total,
        offset=offset,
        limit=limit,
        next_offset=next_offset,
        templates=page_infos,
    )


@router.get("/v1/workflow-templates/{template_id}", response_model=None)
def get_workflow_template(
    template_id: str,
) -> WorkflowTemplateDetailResponse | JSONResponse:
    """取一个模板详情；这是唯一返回完整 Dify YAML 的 catalog 端点。"""
    from ragspine.workflows.catalog import WorkflowCatalog
    from ragspine.workflows.errors import WorkflowTemplateNotFoundError

    request_id = new_request_id()
    try:
        template = WorkflowCatalog.default().get(template_id)
    except WorkflowTemplateNotFoundError as exc:
        return _error_response(
            404,
            type_=exc.code,
            message="workflow template not found",
            request_id=request_id,
        )

    info = _workflow_template_info(template)
    emit_trace(request_id=request_id, op="workflow_catalog.get", found=True)
    return WorkflowTemplateDetailResponse(
        request_id=request_id,
        **info.model_dump(),
        workflow=cast(dict[str, JsonValue], template.workflow),
        yaml=template.yaml,
        preview=cast(dict[str, JsonValue], _workflow_preview_json(template.workflow)),
    )


@router.post("/v1/workflow-scaffold", response_model=None)
def workflow_scaffold(
    req: WorkflowScaffoldRequest,
    matcher: WorkflowMatcherDep,
) -> WorkflowScaffoldResponse | JSONResponse:
    """按描述离线复用或生成 Dify workflow；只返回配置，绝不执行。"""
    from ragspine.workflows.catalog import WorkflowCatalog
    from ragspine.workflows.errors import (
        WorkflowInputError,
        WorkflowMatcherError,
        WorkflowTemplateNotFoundError,
    )
    from ragspine.workflows.scaffold import scaffold_workflow

    request_id = new_request_id()
    catalog = WorkflowCatalog.default()
    if req.template_id is not None:
        try:
            catalog.get(req.template_id)
        except WorkflowTemplateNotFoundError as exc:
            return _error_response(
                404,
                type_=exc.code,
                message="workflow template not found",
                request_id=request_id,
            )

    try:
        result = scaffold_workflow(
            req.description,
            catalog=catalog,
            matcher=matcher,
            template_id=req.template_id,
            reuse=req.reuse,
        )
        response_warnings = list(result.warnings)
    except WorkflowMatcherError:
        result = scaffold_workflow(
            req.description,
            catalog=catalog,
            matcher=_WORKFLOW_LEXICAL_FALLBACK,
            template_id=req.template_id,
            reuse=req.reuse,
        )
        response_warnings = [*result.warnings, _WORKFLOW_MATCHER_FALLBACK_WARNING]
    except WorkflowTemplateNotFoundError as exc:
        return _error_response(
            404,
            type_=exc.code,
            message="workflow template not found",
            request_id=request_id,
        )
    except WorkflowInputError as exc:
        return _error_response(
            400,
            type_=exc.code,
            message=str(exc),
            request_id=request_id,
        )

    emit_trace(
        request_id=request_id,
        op="workflow_scaffold",
        origin=result.origin,
        reused=result.template_id is not None,
        n_warnings=len(response_warnings),
    )
    return WorkflowScaffoldResponse(
        request_id=request_id,
        workflow=cast(dict[str, JsonValue], result.workflow),
        yaml=result.yaml,
        preview=cast(dict[str, JsonValue], _workflow_preview_json(result.workflow)),
        template_id=result.template_id,
        origin=result.origin,
        confidence=result.confidence,
        matcher=result.matcher,
        warnings=response_warnings,
        compatibility=_workflow_compatibility_info(result.compatibility),
        requirements=[_workflow_requirement_info(item) for item in result.requirements],
        source=_workflow_source_metadata(result.source),
    )


# ---------------------------------------------------------------------------
# Studio launch-session（只读）：CLI `workflow serve` 注册，前端凭不透明 token 取回
# ---------------------------------------------------------------------------
# 合法 launch-session id：secrets.token_urlsafe 字符集（URL-safe base64），有界 ≤64。
_LAUNCH_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get("/v1/launch-sessions/{session_id}", response_model=None)
def get_launch_session(
    session_id: str,
    launch_sessions: LaunchSessionsDep,
) -> LaunchSessionResponse | JSONResponse:
    """只读取回一次 launch session：{id, name, yaml}。

    不执行工作流、不落 trace、不 log 内容（隐私不变量：session 内容绝不进
    observability）；未知/超长/非 token 字符的 id 一律同形 404、不回显。
    """
    if _LAUNCH_SESSION_ID_RE.fullmatch(session_id) is None:
        return _error_response(
            404, type_="LaunchSessionNotFound", message="launch session not found"
        )
    session = launch_sessions.get(session_id)
    if session is None:
        return _error_response(
            404, type_="LaunchSessionNotFound", message="launch session not found"
        )
    return LaunchSessionResponse(id=session.session_id, name=session.name, yaml=session.yaml)


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


def _dify_document_yaml(req: Any) -> str:
    """Normalize either HTTP representation through one bounded canonical document."""

    from ragspine.workflows.formats import (
        dump_dify_yaml,
        dump_json,
        parse_workflow,
    )

    if req.workflow is not None:
        # dump_json validates the already-decoded Pydantic value (depth/node/type
        # limits); reparsing applies the same 1 MiB UTF-8 byte limit used by YAML.
        canonical_json = dump_json(
            cast(dict[str, object], req.workflow),
            pretty=False,
        )
        document = parse_workflow(canonical_json, format="json")
    else:
        assert req.yaml is not None
        document = parse_workflow(cast(str, req.yaml), format="yaml")
        # A compact canonical representation is bounded as well, so a terse YAML
        # document cannot expand into an oversized in-memory canonical document.
        canonical_json = dump_json(document, pretty=False)
        document = parse_workflow(canonical_json, format="json")
    return dump_dify_yaml(document)


def _workflow_format_response(request_id: str) -> JSONResponse:
    """Return a stable opaque error; parser failures can contain submitted secrets."""

    return _error_response(
        400,
        type_="workflow.format",
        message="invalid workflow document",
        request_id=request_id,
    )


def _compile_dify_document(
    workflow_yaml: str,
    *,
    target: str = "ragspine",
    fold_answer_question: bool,
    emit_node_traces: bool = False,
) -> Any:
    """Keep the optional Dify compiler lazy-loaded behind one adapter."""

    from ragspine.dify.api import compile_dify_yaml

    return compile_dify_yaml(
        workflow_yaml,
        target=target,
        fold_answer_question=fold_answer_question,
        emit_node_traces=emit_node_traces,
    )


@router.post("/v1/dify/analyze", response_model=None)
def dify_analyze(req: DifyAnalyzeRequest) -> DifyAnalyzeResponse | JSONResponse:
    request_id = new_request_id()
    # 延迟 import：[dify] extra 未装时只这条链报错，不影响其余服务。
    from ragspine.dify.api import analyze as analyze_dify
    from ragspine.dify.errors import DifyCompileError
    from ragspine.workflows.errors import WorkflowFormatError

    try:
        workflow_yaml = _dify_document_yaml(req)
    except WorkflowFormatError:
        return _workflow_format_response(request_id)

    try:
        suggestions = analyze_dify(workflow_yaml)
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
    from ragspine.dify.errors import DifyCompileError
    from ragspine.workflows.errors import WorkflowFormatError

    try:
        workflow_yaml = _dify_document_yaml(req)
    except WorkflowFormatError:
        return _workflow_format_response(request_id)

    try:
        # provider_expr 固定为离线默认；客户端不可注入（防代码注入），运行期再由
        # run_workflow(provider=...) 用服务端 provider 覆盖。
        compiled = _compile_dify_document(
            workflow_yaml,
            target=req.target,
            fold_answer_question=req.fold_answer_question,
        )
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    code = compiled.code
    emit_trace(
        request_id=request_id,
        op="dify.compile",
        target=req.target,
        n_warnings=len(code.warnings),
        n_suggestions=len(compiled.suggestions),
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
            403,
            type_="dify.run_disabled",
            message="dify 工作流执行未开启（设 RAGSPINE_DIFY_RUN_ENABLED=true 开启）",
            request_id=request_id,
        )

    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.runner import (
        DifyRunError,
        DifyTimeoutError,
        run_workflow_isolated,
    )
    from ragspine.service.dify.safety import DifyUnsafeError
    from ragspine.workflows.errors import WorkflowFormatError

    try:
        workflow_yaml = _dify_document_yaml(req)
    except WorkflowFormatError:
        return _workflow_format_response(request_id)

    try:
        compiled = _compile_dify_document(
            workflow_yaml,
            fold_answer_question=req.fold_answer_question,
            emit_node_traces=True,
        )
    except DifyCompileError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    code = compiled.code
    try:
        result = run_workflow_isolated(
            code,
            req.inputs,
            provider,
            timeout_s=config.dify_run_timeout_s,
            isolation=config.dify_run_isolation,
            provider_config=provider_config_dict(config),
        )
    except DifyUnsafeError as exc:
        # L0 静态闸：含 NotImplementedError 骨架 / 不支持节点 / 越权 import -> 422（未执行）。
        return _error_response(422, type_=exc.code, message=str(exc), request_id=request_id)
    except (DifyRunError, DifyTimeoutError) as exc:
        # 执行失败也把已执行节点的 trace（runner 净化后附在 context）带给前端。
        return _error_response(
            400,
            type_=exc.code,
            message=str(exc),
            request_id=request_id,
            node_traces=exc.context.get("node_traces"),
        )

    node_traces = result.pop("__node_traces__", None)
    emit_trace(
        request_id=request_id,
        op="dify.run",
        isolation=config.dify_run_isolation,
        n_result_keys=len(result),
    )
    return DifyRunResponse(
        request_id=request_id,
        result=result,
        warnings=list(code.warnings),
        node_traces=node_traces,
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
            403,
            type_="dify.run_disabled",
            message="dify 工作流执行未开启（设 RAGSPINE_DIFY_RUN_ENABLED=true 开启）",
            request_id=request_id,
        )

    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.safety import DifyUnsafeError, assert_runnable
    from ragspine.workflows.errors import WorkflowFormatError

    try:
        workflow_yaml = _dify_document_yaml(req)
    except WorkflowFormatError:
        return _workflow_format_response(request_id)

    try:
        compiled = _compile_dify_document(
            workflow_yaml,
            fold_answer_question=req.fold_answer_question,
            emit_node_traces=True,
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


# ---------------------------------------------------------------------------
# 管线拓扑导出（供 Studio 可视化；静态派生，零执行、零重资源）
# ---------------------------------------------------------------------------
_TOPOLOGY_SCOPES = ("agent", "retriever", "service")


@router.get("/v1/topology", response_model=None)
def get_topology(request: Request, scope: str = "agent") -> TopologyResponse | JSONResponse:
    """按 scope 导出静态管线拓扑：agent（完整请求流）| retriever（默认检索骨架）| service（服务层）。

    agent / retriever scope 使用轻量哨兵描述完整参考架构，不实例化检索资源；
    service scope 从 app.state 反射（duck-typed）。
    """
    request_id = new_request_id()
    if scope == "agent":
        graph = agent_topology(narrative_retriever=object())
    elif scope == "retriever":
        graph = retriever_topology(object())
    elif scope == "service":
        graph = service_topology(request.app)
    else:
        return _error_response(
            400,
            type_="InvalidScope",
            message=f"未知 scope: {scope!r}（可选: {', '.join(_TOPOLOGY_SCOPES)}）",
            request_id=request_id,
        )

    emit_trace(
        request_id=request_id,
        op="topology",
        scope=scope,
        n_nodes=len(graph.nodes),
        n_edges=len(graph.edges),
    )
    return TopologyResponse(
        request_id=request_id,
        title=graph.title,
        nodes=[
            TopologyNodeInfo(id=n.id, label=n.label, kind=n.kind, domain=n.domain, symbol=n.symbol)
            for n in graph.nodes
        ],
        edges=[
            TopologyEdgeInfo(src=e.src, dst=e.dst, label=e.label, kind=e.kind) for e in graph.edges
        ],
    )


# ---------------------------------------------------------------------------
# n8n workflow 兼容层：n8n JSON ↔ Dify DSL 转换 / 运行
#
# convert（纯转换，不执行，安全）→ run（n8n→dify 后调用 dify_run，完整复用其编译 +
# 受限执行与信任边界，不重写）。ragspine.n8n 延迟 import；N8nConvertError（code n8n.*）
# 整形为 400。PyYAML 经 [dify] extra 带入，同样延迟 import。
# ---------------------------------------------------------------------------
def _dify_dict_to_yaml(doc: dict[str, Any]) -> str:
    """Dify DSL dict → YAML 文本（PyYAML 延迟 import，与 dify parse 段同一依赖假设）。"""
    import yaml

    text: str = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    return text


@router.post("/v1/n8n/convert", response_model=None)
def n8n_convert(req: N8nConvertRequest) -> N8nConvertResponse | JSONResponse:
    """n8n workflow JSON ↔ Dify DSL 双向转换。无法语义映射处进 warnings，绝不静默丢弃。"""
    request_id = new_request_id()
    from ragspine.n8n.api import dify_to_n8n, n8n_to_dify
    from ragspine.n8n.errors import N8nConvertError

    try:
        if req.direction == "n8n_to_dify":
            workflow, warnings = n8n_to_dify(req.workflow)
            yaml_text: str | None = _dify_dict_to_yaml(workflow)
        else:
            workflow, warnings = dify_to_n8n(req.workflow)
            yaml_text = None
    except N8nConvertError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    emit_trace(
        request_id=request_id,
        op="n8n.convert",
        direction=req.direction,
        n_nodes=len(workflow.get("nodes", []) or []),
        n_warnings=len(warnings),
    )
    return N8nConvertResponse(
        request_id=request_id,
        workflow=workflow,
        yaml=yaml_text,
        warnings=warnings,
    )


@router.post("/v1/n8n/run", response_model=None)
def n8n_run(
    req: N8nRunRequest,
    config: ConfigDep,
    provider: ProviderDep,
) -> N8nRunResponse | JSONResponse:
    """n8n workflow → dify DSL → 完整复用 dify_run（编译 + 受限执行，信任边界）。

    受同一开关管控（RAGSPINE_DIFY_RUN_ENABLED，未开启 -> 403）；转换失败
    （N8nConvertError，code n8n.*）-> 400；其后错误分级（编译 400 / L0 闸 422 /
    执行失败超时 400）与响应形状（+ convert_warnings）均由 dify_run 决定。
    """
    request_id = new_request_id()

    # 信任边界开关先行（与 dify_run 同一开关；转换前快速失败，不泄漏转换结果）。
    if not config.dify_run_enabled:
        return _error_response(
            403,
            type_="dify.run_disabled",
            message="dify 工作流执行未开启（设 RAGSPINE_DIFY_RUN_ENABLED=true 开启）",
            request_id=request_id,
        )

    from ragspine.n8n.api import n8n_to_dify
    from ragspine.n8n.errors import N8nConvertError

    try:
        dify_doc, convert_warnings = n8n_to_dify(req.workflow)
    except N8nConvertError as exc:
        return _error_response(400, type_=exc.code, message=str(exc), request_id=request_id)

    response = dify_run(
        DifyRunRequest(yaml=_dify_dict_to_yaml(dify_doc), inputs=req.inputs),
        config,
        provider,
    )
    if isinstance(response, JSONResponse):
        return response  # dify_run 已整形（400/422 等），原样透传
    emit_trace(
        request_id=response.request_id,
        op="n8n.run",
        n_convert_warnings=len(convert_warnings),
    )
    return N8nRunResponse(
        **response.model_dump(),
        convert_warnings=convert_warnings,
    )
