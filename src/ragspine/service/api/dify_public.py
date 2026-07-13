"""Dify 官方 Workflow App API 形状克隆：现有 dify SDK / 客户端零改动直连本服务。

语义对齐 dify"服务端已发布 app、客户端只带 inputs 调用"：Bearer app-key 即选中
服务端注册的 workflow YAML（env RAGSPINE_DIFY_PUBLIC_APPS，`key=path;key=path`），
内部完整复用现有 dify 编译 + 受限执行管线（compile_dify_yaml + run_workflow_isolated，
同受 RAGSPINE_DIFY_RUN_ENABLED 信任边界开关管控）。

对外形状以 dify 官方为准（web/.../template_workflow.en.mdx + api/libs/external_api.py）：
- POST /v1/workflows/run：blocking -> {workflow_run_id, task_id, data{...}}；
  streaming -> SSE `data: {...}\\n\\n`（workflow_started → node_started/node_finished
  对 → workflow_finished）。执行是整体完成后按 trace 顺序回放，skipped 节点不发事件。
- 执行失败（编译错/运行错）与 dify 行为一致：HTTP 200 + data.status="failed" + error。
- 错误体 {code, message, status}：401 unauthorized / 400 invalid_param /
  400 app_unavailable / 404 not_found。
- GET /v1/workflows/run/{id}：进程内 LRU（上限 _MAX_RUNS）缓存最近 run 摘要。
- GET /v1/info、/v1/parameters：从注册 YAML（app 段 + start 节点 variables）派生。

本文件自包含（schemas 不进 schemas.py，路由不进 routes.py）；app.py 仅 include_router。
"""

import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.api.dependencies import get_config, get_provider
from ragspine.service.config import ServiceConfig, provider_config_dict

router = APIRouter()

# DI 别名（与 routes.py 同款 Annotated 形式，测试可经 dependency_overrides 注入）。
ConfigDep = Annotated[ServiceConfig, Depends(get_config)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]

# 进程内 run 摘要 LRU 上限（挂在 app.state，测试间天然隔离）。
_MAX_RUNS = 100

_RESPONSE_MODES = ("blocking", "streaming")


# ---------------------------------------------------------------------------
# schemas（自包含；对外形状即 dify 官方形状）
# ---------------------------------------------------------------------------
class WorkflowRunPublicRequest(BaseModel):
    """POST /v1/workflows/run 请求体（官方字段；多余字段忽略，与 dify 一致宽容）。"""

    inputs: dict[str, Any] = Field(default_factory=dict)
    response_mode: str = "blocking"   # dify 官方缺省即 blocking
    user: str = ""
    files: list[Any] = Field(default_factory=list)  # 接受但不消费（本服务无文件输入）


class WorkflowRunResultData(BaseModel):
    """blocking 响应的 data 段（官方 CompletionResponse.data）。"""

    id: str
    workflow_id: str
    status: str                       # "succeeded" | "failed"
    outputs: dict[str, Any] | None = None
    error: str | None = None
    elapsed_time: float = 0.0
    total_tokens: int = 0
    total_steps: int = 0
    created_at: int = 0
    finished_at: int = 0


class WorkflowRunBlockingResponse(BaseModel):
    """官方 blocking CompletionResponse：{workflow_run_id, task_id, data}。"""

    task_id: str
    workflow_run_id: str
    data: WorkflowRunResultData


class WorkflowRunDetailResponse(BaseModel):
    """GET /v1/workflows/run/{id} 官方响应（扁平 run 摘要）。"""

    id: str
    workflow_id: str
    status: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] | None = None
    error: str | None = None
    total_steps: int = 0
    total_tokens: int = 0
    created_at: int = 0
    finished_at: int = 0
    elapsed_time: float = 0.0


# ---------------------------------------------------------------------------
# 官方错误形状 / app 注册表 / 鉴权
# ---------------------------------------------------------------------------
def _dify_error(status_code: int, code: str, message: str) -> JSONResponse:
    """dify 官方错误体 {code, message, status}（api/libs/external_api.py 形状）。"""
    headers = {"WWW-Authenticate": 'Bearer realm="api"'} if status_code == 401 else None
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "status": status_code},
        headers=headers,
    )


def _parse_public_apps(raw: str) -> dict[str, str]:
    """`key1=/path/a.yml;key2=C:\\x\\b.yml` -> {key: path}。

    `;` 分隔条目、首个 `=` 分隔 key 与路径（路径内可再含 `=`；Windows 盘符的 `:`
    不受影响）。空条目 / 缺 `=` 的条目跳过。
    """
    apps: dict[str, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, _, path = entry.partition("=")
        key, path = key.strip(), path.strip()
        if key and path:
            apps[key] = path
    return apps


def _load_app(
    request: Request, config: ServiceConfig
) -> tuple[str, str, str] | JSONResponse:
    """Bearer 鉴权 + 注册表选 app + 读 YAML -> (api_key, yaml_path, yaml_text)。

    未带 / 非 Bearer / 未注册 key（含未配置任何 app）-> 401 unauthorized；
    注册路径读不到 -> 400 app_unavailable（服务端配置错误，给清晰错误）。
    """
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return _dify_error(401, "unauthorized", "Access token is invalid")

    apps = _parse_public_apps(config.dify_public_apps)
    yaml_path = apps.get(token)
    if yaml_path is None:
        return _dify_error(401, "unauthorized", "Access token is invalid")

    try:
        yaml_text = Path(yaml_path).read_text(encoding="utf-8")
    except OSError as exc:
        return _dify_error(
            400, "app_unavailable",
            f"registered workflow file unreadable: {yaml_path} ({exc})",
        )
    return token, yaml_path, yaml_text


def _workflow_id(yaml_path: str) -> str:
    """按注册路径派生稳定 workflow_id（同一 app 恒定，不泄露 api key）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ragspine:dify-public:{yaml_path}"))


# ---------------------------------------------------------------------------
# 执行复用：现有 dify 编译 + 受限运行管线（不重写、不重构）
# ---------------------------------------------------------------------------
@dataclass
class _RunOutcome:
    status: str                        # "succeeded" | "failed"
    outputs: dict[str, Any] | None
    error: str | None
    traces: list[dict[str, Any]]       # 净化后的 NodeTrace dict（可为空）
    elapsed_time: float                # 墙钟秒


def _execute_workflow(
    yaml_text: str,
    inputs: dict[str, Any],
    config: ServiceConfig,
    provider: LLMProvider,
) -> _RunOutcome:
    """复用 routes.dify_run 同款管线；一切执行链失败折叠为 status="failed"。

    dify 语义下 app 已发布、客户端只管 inputs——编译错 / L0 闸拒 / 运行错 / 超时
    对客户端统一表现为一次失败的 run（HTTP 200 + data.status=failed，与 dify 一致）。
    """
    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.runner import (
        DifyRunError,
        DifyTimeoutError,
        run_workflow_isolated,
    )
    from ragspine.service.dify.safety import DifyUnsafeError

    started = time.perf_counter()

    def _elapsed() -> float:
        return time.perf_counter() - started

    try:
        compiled = compile_dify_yaml(yaml_text, emit_node_traces=True)
    except DifyCompileError as exc:
        return _RunOutcome("failed", None, f"{exc.code}: {exc}", [], _elapsed())

    try:
        result = run_workflow_isolated(
            compiled.code, inputs, provider,
            timeout_s=config.dify_run_timeout_s,
            isolation=config.dify_run_isolation,
            provider_config=provider_config_dict(config),
        )
    except DifyUnsafeError as exc:
        return _RunOutcome("failed", None, f"{exc.code}: {exc}", [], _elapsed())
    except (DifyRunError, DifyTimeoutError) as exc:
        raw_traces = exc.context.get("node_traces")
        traces = raw_traces if isinstance(raw_traces, list) else []
        return _RunOutcome("failed", None, f"{exc.code}: {exc}", traces, _elapsed())

    traces = result.pop("__node_traces__", None) or []
    return _RunOutcome("succeeded", result, None, traces, _elapsed())


def _executed_traces(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """回放用 trace：skipped 节点不发事件、不计步。"""
    return [t for t in traces if t.get("status") != "skipped"]


# ---------------------------------------------------------------------------
# run 摘要 LRU（进程内，挂 app.state；按 api_key 归属隔离）
# ---------------------------------------------------------------------------
def _run_store(request: Request) -> "OrderedDict[str, dict[str, Any]]":
    store = getattr(request.app.state, "dify_public_runs", None)
    if store is None:
        store = OrderedDict()
        request.app.state.dify_public_runs = store
    return store


def _store_run(
    request: Request, api_key: str, detail: WorkflowRunDetailResponse
) -> None:
    store = _run_store(request)
    store[detail.id] = {"api_key": api_key, "detail": detail}
    store.move_to_end(detail.id)
    while len(store) > _MAX_RUNS:
        store.popitem(last=False)


# ---------------------------------------------------------------------------
# SSE 回放：执行完成后按 trace index 顺序重建官方事件流
# ---------------------------------------------------------------------------
def _sse_events(
    *,
    task_id: str,
    run_id: str,
    workflow_id: str,
    outcome: _RunOutcome,
    created_at: int,
    finished_at: int,
) -> list[dict[str, Any]]:
    def _event(kind: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "event": kind,
            "task_id": task_id,
            "workflow_run_id": run_id,
            "data": data,
        }

    events = [
        _event(
            "workflow_started",
            {"id": run_id, "workflow_id": workflow_id, "created_at": created_at},
        )
    ]
    executed = _executed_traces(outcome.traces)
    predecessor: str | None = None
    for i, trace in enumerate(executed):
        node_exec_id = str(uuid.uuid4())
        started_data = {
            "id": node_exec_id,
            "node_id": trace.get("node_id", ""),
            "node_type": trace.get("node_type", ""),
            "title": trace.get("title", ""),
            "index": i + 1,                       # dify 的 index 从 1 起
            "predecessor_node_id": predecessor,
            "inputs": trace.get("inputs"),
            "created_at": created_at,
        }
        events.append(_event("node_started", started_data))
        finished_data = dict(started_data)
        finished_data.update(
            {
                "process_data": None,
                "outputs": trace.get("outputs"),
                "status": trace.get("status", "failed"),
                "error": trace.get("error"),
                "elapsed_time": float(trace.get("elapsed_ms", 0.0)) / 1000.0,
                "execution_metadata": None,
            }
        )
        events.append(_event("node_finished", finished_data))
        predecessor = trace.get("node_id", "")
    events.append(
        _event(
            "workflow_finished",
            {
                "id": run_id,
                "workflow_id": workflow_id,
                "status": outcome.status,
                "outputs": outcome.outputs,
                "error": outcome.error,
                "elapsed_time": outcome.elapsed_time,
                "total_tokens": 0,
                "total_steps": len(executed),
                "created_at": created_at,
                "finished_at": finished_at,
            },
        )
    )
    return events


def _sse_iter(events: list[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False, default=repr)}\n\n"


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.post("/v1/workflows/run", response_model=None)
def workflows_run(
    req: WorkflowRunPublicRequest,
    request: Request,
    config: ConfigDep,
    provider: ProviderDep,
) -> WorkflowRunBlockingResponse | StreamingResponse | JSONResponse:
    loaded = _load_app(request, config)
    if isinstance(loaded, JSONResponse):
        return loaded
    api_key, yaml_path, yaml_text = loaded

    if req.response_mode not in _RESPONSE_MODES:
        return _dify_error(
            400, "invalid_param",
            f"response_mode must be one of {list(_RESPONSE_MODES)}",
        )
    if not req.user:
        return _dify_error(400, "invalid_param", "user is required")
    # 与 /v1/dify/run 同一信任边界开关；关闭时按 dify 官方形状报 app 不可用。
    if not config.dify_run_enabled:
        return _dify_error(
            400, "app_unavailable",
            "workflow execution is disabled on this server "
            "(set RAGSPINE_DIFY_RUN_ENABLED=true)",
        )

    run_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    workflow_id = _workflow_id(yaml_path)
    created_at = int(time.time())
    outcome = _execute_workflow(yaml_text, req.inputs, config, provider)
    finished_at = int(time.time())
    total_steps = len(_executed_traces(outcome.traces))

    _store_run(
        request, api_key,
        WorkflowRunDetailResponse(
            id=run_id,
            workflow_id=workflow_id,
            status=outcome.status,
            inputs=req.inputs,
            outputs=outcome.outputs,
            error=outcome.error,
            total_steps=total_steps,
            total_tokens=0,
            created_at=created_at,
            finished_at=finished_at,
            elapsed_time=outcome.elapsed_time,
        ),
    )

    if req.response_mode == "streaming":
        events = _sse_events(
            task_id=task_id, run_id=run_id, workflow_id=workflow_id,
            outcome=outcome, created_at=created_at, finished_at=finished_at,
        )
        return StreamingResponse(_sse_iter(events), media_type="text/event-stream")

    return WorkflowRunBlockingResponse(
        task_id=task_id,
        workflow_run_id=run_id,
        data=WorkflowRunResultData(
            id=run_id,
            workflow_id=workflow_id,
            status=outcome.status,
            outputs=outcome.outputs,
            error=outcome.error,
            elapsed_time=outcome.elapsed_time,
            total_tokens=0,
            total_steps=total_steps,
            created_at=created_at,
            finished_at=finished_at,
        ),
    )


@router.get("/v1/workflows/run/{workflow_run_id}", response_model=None)
def workflows_run_detail(
    workflow_run_id: str,
    request: Request,
    config: ConfigDep,
) -> WorkflowRunDetailResponse | JSONResponse:
    loaded = _load_app(request, config)
    if isinstance(loaded, JSONResponse):
        return loaded
    api_key = loaded[0]

    record = _run_store(request).get(workflow_run_id)
    if record is None or record["api_key"] != api_key:
        return _dify_error(404, "not_found", "Workflow run not found")
    detail: WorkflowRunDetailResponse = record["detail"]
    return detail


def _parse_app_yaml(yaml_text: str) -> dict[str, Any]:
    """注册 YAML -> dict（PyYAML 经 [dify] extra 带入，延迟 import；坏 YAML 给 {}）。"""
    import yaml

    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


@router.get("/v1/info", response_model=None)
def app_info(request: Request, config: ConfigDep) -> JSONResponse:
    loaded = _load_app(request, config)
    if isinstance(loaded, JSONResponse):
        return loaded
    doc = _parse_app_yaml(loaded[2])
    app_raw = doc.get("app")
    app_section = app_raw if isinstance(app_raw, dict) else {}
    return JSONResponse(
        content={
            "name": str(app_section.get("name", "")),
            "description": str(app_section.get("description", "") or ""),
            "tags": [],
            "mode": str(app_section.get("mode", "workflow")),
            "author_name": None,
        }
    )


def _start_variables(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """workflow.graph.nodes 里 start 节点的 variables（形状防御，坏结构给 []）。"""
    workflow = doc.get("workflow")
    if not isinstance(workflow, dict):
        return []
    graph = workflow.get("graph")
    if not isinstance(graph, dict):
        return []
    for node in graph.get("nodes") or []:
        data = node.get("data") if isinstance(node, dict) else None
        if isinstance(data, dict) and data.get("type") == "start":
            variables = data.get("variables")
            return [v for v in variables or [] if isinstance(v, dict)]
    return []


@router.get("/v1/parameters", response_model=None)
def app_parameters(request: Request, config: ConfigDep) -> JSONResponse:
    loaded = _load_app(request, config)
    if isinstance(loaded, JSONResponse):
        return loaded
    doc = _parse_app_yaml(loaded[2])

    user_input_form: list[dict[str, Any]] = []
    for var in _start_variables(doc):
        control_type = str(var.get("type", "text-input"))
        item: dict[str, Any] = {
            "label": str(var.get("label", var.get("variable", ""))),
            "variable": str(var.get("variable", "")),
            "required": bool(var.get("required", False)),
            "default": var.get("default", ""),
        }
        if control_type == "select":
            item["options"] = list(var.get("options") or [])
        user_input_form.append({control_type: item})

    return JSONResponse(
        content={
            "user_input_form": user_input_form,
            # 本服务不消费文件输入；按官方形状如实报 disabled。
            "file_upload": {
                "image": {
                    "enabled": False,
                    "number_limits": 3,
                    "transfer_methods": ["local_file", "remote_url"],
                }
            },
            "system_parameters": {
                "file_size_limit": 15,
                "image_file_size_limit": 10,
                "audio_file_size_limit": 50,
                "video_file_size_limit": 100,
            },
        }
    )
