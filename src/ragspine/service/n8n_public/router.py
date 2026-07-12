"""n8n 公共 REST API 形状克隆：n8n API 客户端/脚本零改动直连本服务。

对外形状以 n8n 官方 Public API 为准（GitHub OpenAPI spec + 源码）：
- /api/v1/*：`X-N8N-API-KEY` 鉴权，错误体恒 `{"message": ...}` 单键；服务端未配置
  RAGSPINE_N8N_API_KEY 时一律 401（公共 API 必须显式启用）。
- workflows：CRUD（POST/PUT body 严格校验，message 文本对齐 express-openapi-validator）
  + activate/deactivate + offset 型 cursor 分页（base64 JSON {"limit","offset"}，
  createdAt 升序）；官方参数 tags/projectId/excludePinnedData 接受但忽略。
- executions：列表（id 降序、lastId 型 cursor：返回 id 严格小于 lastId 的下一页）/
  详情 / 删除；`includeData=true` 时才带 data。
- /webhook/{path}（GET|POST，无鉴权，与 n8n 一致）：匹配 active workflow 中
  `n8n-nodes-base.webhook` 节点（parameters.path 两侧 strip "/" 后等于请求 path、
  httpMethod 缺省 GET、可为字符串或列表），经 n8n→dify 转换完整复用 routes.dify_run
  管线执行，每次执行落一条 execution 记录。

webhook inputs 映射规则：query params dict 与 JSON body（若为 dict）合并作为 dify
start inputs，键冲突时 body 优先；body 非 dict 或非 JSON 则只用 query params。

本文件自包含（schemas/校验不进 schemas.py，路由不进 routes.py）；app.py 仅
include_router。重依赖（ragspine.n8n、routes.dify_run）在 handler 内延迟 import。
"""

import base64
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.api.dependencies import get_config, get_provider
from ragspine.service.config import ServiceConfig
from ragspine.service.n8n_public.store import N8nStore

api_router = APIRouter(prefix="/api/v1")
webhook_router = APIRouter(prefix="/webhook")

# DI 别名（与 routes.py 同款 Annotated 形式，测试可经 dependency_overrides 注入）。
ConfigDep = Annotated[ServiceConfig, Depends(get_config)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]

_WEBHOOK_NODE_TYPE = "n8n-nodes-base.webhook"
_WEBHOOK_HINT = (
    "The workflow must be active for a production URL to run successfully. "
    "You can activate the workflow using the toggle in the top-right of the editor. "
    "Note that unlike test URL calls, production URL calls aren't shown on the canvas "
    "(only in the executions list)"
)

# n8n workflow body 严格校验（additionalProperties: false + readOnly，拒绝而非忽略）。
_READONLY_FIELDS = (
    "id", "active", "createdAt", "updatedAt", "tags",
    "isArchived", "versionId", "triggerCount", "meta",
)
_REQUIRED_FIELDS = ("name", "nodes", "connections", "settings")
_OPTIONAL_FIELDS = ("staticData",)


# ---------------------------------------------------------------------------
# 通用 helpers：错误体 / 鉴权 / 存储 / 时间戳 / cursor
# ---------------------------------------------------------------------------
def _message(status_code: int, message: str) -> JSONResponse:
    """n8n 形状错误体（恒 {"message": ...} 单键；不走 HTTPException 的 {"detail"}）。"""
    return JSONResponse(status_code=status_code, content={"message": message})


def _not_found() -> JSONResponse:
    """所有 /api/v1 的 404 都是这个精确文本。"""
    return _message(404, "Not Found")


def _check_api_key(request: Request, config: ServiceConfig) -> JSONResponse | None:
    """/api/v1/* 鉴权；每个 handler 开头调用，非 None 直接 return。"""
    if not config.n8n_api_key:
        return _message(
            401, "n8n public API is disabled: set RAGSPINE_N8N_API_KEY to enable it"
        )
    supplied = request.headers.get("X-N8N-API-KEY")
    if supplied is None:
        return _message(401, "'X-N8N-API-KEY' header required")
    if supplied != config.n8n_api_key:
        return _message(401, "unauthorized")
    return None


def _store(config: ServiceConfig) -> N8nStore:
    """每请求新建（纯文件操作、无状态，不必缓存）。"""
    return N8nStore(Path(config.n8n_store_path))


def _now_iso() -> str:
    """n8n 时间戳格式：UTC 毫秒 + Z（如 2024-01-15T10:30:00.000Z）。"""
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _clamp_limit(limit: int) -> int:
    """limit 默认 100、上限 250、下限 1。"""
    return max(1, min(limit, 250))


def _encode_cursor(payload: dict[str, int]) -> str:
    return base64.b64encode(json.dumps(payload).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any] | None:
    """base64 JSON cursor -> dict；任何解不开的形状给 None（调用方整形 400）。"""
    try:
        decoded = json.loads(base64.b64decode(cursor.encode("ascii"), validate=True))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _validate_workflow_body(body: dict[str, Any]) -> JSONResponse | None:
    """POST/PUT body 手工校验（pydantic extra=forbid 给 422，形状不对）。

    次序与 n8n 一致：readOnly 字段 -> 未知字段 -> 缺必填。
    """
    for field in _READONLY_FIELDS:
        if field in body:
            return _message(400, f"request/body/{field} is read-only")
    allowed = set(_REQUIRED_FIELDS) | set(_OPTIONAL_FIELDS)
    for key in body:
        if key not in allowed:
            return _message(
                400, "request/body must NOT have additional properties"
            )
    for field in _REQUIRED_FIELDS:
        if field not in body:
            return _message(
                400, f"request/body must have required property '{field}'"
            )
    return None


def _execution_view(record: dict[str, Any], include_data: bool) -> dict[str, Any]:
    """execution 对外视图：includeData=true 才带 data。"""
    if include_data:
        return dict(record)
    return {k: v for k, v in record.items() if k != "data"}


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------
@api_router.get("/workflows", response_model=None)
def list_workflows(
    request: Request,
    config: ConfigDep,
    active: bool | None = None,
    name: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any] | JSONResponse:
    """列表：active/name 过滤 + offset 型 cursor 分页（createdAt 升序稳定序）。

    官方其余参数（tags/projectId/excludePinnedData）接受但忽略（FastAPI 不声明即忽略）。
    """
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    records = _store(config).list_workflows(active=active, name=name)
    limit = _clamp_limit(limit)
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if decoded is None or not isinstance(decoded.get("offset"), int):
            return _message(400, "invalid cursor")
        offset = decoded["offset"]
        if isinstance(decoded.get("limit"), int):
            limit = _clamp_limit(decoded["limit"])
    page = records[offset:offset + limit]
    next_cursor = (
        _encode_cursor({"limit": limit, "offset": offset + limit})
        if offset + limit < len(records) else None
    )
    return {"data": page, "nextCursor": next_cursor}


@api_router.post("/workflows", response_model=None)
def create_workflow(
    body: dict[str, Any], request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    invalid = _validate_workflow_body(body)
    if invalid is not None:
        return invalid
    now = _now_iso()
    workflow: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "name": body["name"],
        "active": False,
        "createdAt": now,
        "updatedAt": now,
        "nodes": body["nodes"],
        "connections": body["connections"],
        "settings": body["settings"],
        "staticData": body.get("staticData"),
        "tags": [],
    }
    _store(config).save_workflow(workflow)
    return workflow


@api_router.get("/workflows/{workflow_id}", response_model=None)
def get_workflow(
    workflow_id: str, request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    workflow = _store(config).get_workflow(workflow_id)
    if workflow is None:
        return _not_found()
    return workflow


@api_router.put("/workflows/{workflow_id}", response_model=None)
def update_workflow(
    workflow_id: str, body: dict[str, Any], request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    """全量替换（校验与 POST 完全相同）；保留 id/active/createdAt/tags，刷新 updatedAt。"""
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    invalid = _validate_workflow_body(body)
    if invalid is not None:
        return invalid
    store = _store(config)
    existing = store.get_workflow(workflow_id)
    if existing is None:
        return _not_found()
    updated: dict[str, Any] = {
        "id": existing["id"],
        "name": body["name"],
        "active": existing.get("active", False),
        "createdAt": existing.get("createdAt"),
        "updatedAt": _now_iso(),
        "nodes": body["nodes"],
        "connections": body["connections"],
        "settings": body["settings"],
        "staticData": body.get("staticData"),
        "tags": existing.get("tags", []),
    }
    store.save_workflow(updated)
    return updated


@api_router.delete("/workflows/{workflow_id}", response_model=None)
def delete_workflow(
    workflow_id: str, request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    """200 返回被删除的 workflow 对象。"""
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    deleted = _store(config).delete_workflow(workflow_id)
    if deleted is None:
        return _not_found()
    return deleted


def _set_active(
    workflow_id: str, active: bool, request: Request, config: ServiceConfig
) -> dict[str, Any] | JSONResponse:
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    store = _store(config)
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        return _not_found()
    workflow["active"] = active
    workflow["updatedAt"] = _now_iso()  # 与 n8n 一致：翻转时刷新
    store.save_workflow(workflow)
    return workflow


@api_router.post("/workflows/{workflow_id}/activate", response_model=None)
def activate_workflow(
    workflow_id: str, request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    return _set_active(workflow_id, True, request, config)


@api_router.post("/workflows/{workflow_id}/deactivate", response_model=None)
def deactivate_workflow(
    workflow_id: str, request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    return _set_active(workflow_id, False, request, config)


# ---------------------------------------------------------------------------
# executions
# ---------------------------------------------------------------------------
@api_router.get("/executions", response_model=None)
def list_executions(
    request: Request,
    config: ConfigDep,
    workflow_id: Annotated[str | None, Query(alias="workflowId")] = None,
    status: str | None = None,
    include_data: Annotated[bool, Query(alias="includeData")] = False,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any] | JSONResponse:
    """列表：id 降序（最新在前）+ lastId 型 cursor（返回 id 严格小于 lastId 的下一页）。"""
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    records = _store(config).list_executions(workflow_id=workflow_id, status=status)
    limit = _clamp_limit(limit)
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if decoded is None or not isinstance(decoded.get("lastId"), int):
            return _message(400, "invalid cursor")
        last_id = decoded["lastId"]
        records = [r for r in records if int(r.get("id", 0)) < last_id]
        if isinstance(decoded.get("limit"), int):
            limit = _clamp_limit(decoded["limit"])
    page = records[:limit]
    next_cursor = (
        _encode_cursor({"lastId": int(page[-1]["id"]), "limit": limit})
        if len(records) > limit else None
    )
    return {
        "data": [_execution_view(r, include_data) for r in page],
        "nextCursor": next_cursor,
    }


@api_router.get("/executions/{execution_id}", response_model=None)
def get_execution(
    execution_id: str,
    request: Request,
    config: ConfigDep,
    include_data: Annotated[bool, Query(alias="includeData")] = False,
) -> dict[str, Any] | JSONResponse:
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    record = (
        _store(config).get_execution(int(execution_id))
        if execution_id.isdigit() else None
    )
    if record is None:
        return _not_found()
    return _execution_view(record, include_data)


@api_router.delete("/executions/{execution_id}", response_model=None)
def delete_execution(
    execution_id: str, request: Request, config: ConfigDep
) -> dict[str, Any] | JSONResponse:
    """200 返回被删除的 execution 对象。"""
    denied = _check_api_key(request, config)
    if denied is not None:
        return denied
    deleted = (
        _store(config).delete_execution(int(execution_id))
        if execution_id.isdigit() else None
    )
    if deleted is None:
        return _not_found()
    return _execution_view(deleted, False)


# ---------------------------------------------------------------------------
# webhook 触发（不属于 public API，无鉴权，与 n8n 一致）
# ---------------------------------------------------------------------------
def _merge_inputs(query_params: Mapping[str, Any], body: Any) -> dict[str, Any]:
    """query params 与 JSON body（若为 dict）合并为 dify start inputs；冲突时 body 优先。"""
    merged: dict[str, Any] = dict(query_params)
    if isinstance(body, dict):
        merged.update(body)
    return merged


def _match_webhook(store: N8nStore, path: str, method: str) -> dict[str, Any] | None:
    """在 active workflows 中找第一个匹配的 webhook 节点所在 workflow。

    匹配条件：节点 type 为 n8n-nodes-base.webhook、parameters.path（两侧 strip "/"
    后）等于请求 path、parameters.httpMethod（缺省默认 "GET"；字符串或列表，列表则
    任一匹配）等于请求 method。
    """
    wanted = path.strip("/")
    for workflow in store.list_workflows(active=True):
        for node in workflow.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") != _WEBHOOK_NODE_TYPE:
                continue
            params = node.get("parameters") or {}
            if str(params.get("path", "")).strip("/") != wanted:
                continue
            methods = params.get("httpMethod", "GET")
            if isinstance(methods, str):
                methods = [methods]
            if method.upper() in {str(m).upper() for m in methods}:
                return workflow
    return None


def _run_webhook_workflow(
    workflow: dict[str, Any],
    inputs: dict[str, Any],
    config: ServiceConfig,
    provider: LLMProvider,
) -> JSONResponse:
    """存储中的原始 n8n JSON → n8n_to_dify → 复用 routes.dify_run（不重写管线）。

    每次执行（转换失败/编译失败/执行失败/成功）都落一条 execution：成功
    status="success" 且 data=dify 结果 dict；失败 status="error" 且 data 放错误信息。
    """
    # 重依赖延迟 import（与 routes.py 风格一致；顶层不 import routes 避免循环）。
    from ragspine.n8n.api import n8n_to_dify
    from ragspine.n8n.errors import N8nConvertError
    from ragspine.service.api.routes import _dify_dict_to_yaml, dify_run
    from ragspine.service.api.schemas import DifyRunRequest

    store = _store(config)
    started_at = _now_iso()

    def _record(status: str, data: Any) -> None:
        store.create_execution({
            "finished": status == "success",
            "mode": "webhook",
            "retryOf": None,
            "retrySuccessId": None,
            "startedAt": started_at,
            "stoppedAt": _now_iso(),
            "workflowId": workflow.get("id"),
            "waitTill": None,
            "status": status,
            "customData": {},
            "data": data,
        })

    source = {
        key: workflow.get(key) for key in ("name", "nodes", "connections", "settings")
    }
    try:
        dify_doc, _warnings = n8n_to_dify(source)
    except N8nConvertError as exc:
        _record("error", {"error": {"type": exc.code, "message": str(exc)}})
        return _message(500, "Error in workflow")

    response = dify_run(
        DifyRunRequest(yaml=_dify_dict_to_yaml(dify_doc), inputs=inputs),
        config, provider,
    )
    if isinstance(response, JSONResponse):
        # dify_run 已整形的错误信封（编译 400 / L0 闸 422 / 执行失败超时 400）。
        error_payload = json.loads(bytes(response.body))
        _record(
            "error",
            error_payload if isinstance(error_payload, dict)
            else {"error": error_payload},
        )
        return _message(500, "Error in workflow")

    result = dict(response.result)
    _record("success", result)
    return JSONResponse(content=result)


@webhook_router.api_route(
    "/{path:path}", methods=["GET", "POST"], response_model=None
)
async def webhook_trigger(
    path: str, request: Request, config: ConfigDep, provider: ProviderDep
) -> JSONResponse:
    """无鉴权 webhook 触发（path 可含斜杠）。

    inputs 映射：query params dict 与 JSON body（若为 dict）合并作为 dify start
    inputs，键冲突时 body 优先；body 非 dict 或非 JSON 则只用 query params。
    """
    workflow = _match_webhook(_store(config), path, request.method)
    if workflow is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": 404,
                "message": (
                    f'The requested webhook "{request.method} {path}" '
                    "is not registered."
                ),
                "hint": _WEBHOOK_HINT,
            },
        )
    if not config.dify_run_enabled:
        return _message(
            503,
            "workflow execution is disabled: "
            "set RAGSPINE_DIFY_RUN_ENABLED=true to enable it",
        )
    try:
        body = await request.json()
    except ValueError:
        body = None  # body 非 JSON -> 只用 query params
    inputs = _merge_inputs(dict(request.query_params), body)
    # 同步执行段进线程池：与 sync def 路由的默认行为等价，执行期不阻塞事件循环。
    return await run_in_threadpool(
        _run_webhook_workflow, workflow, inputs, config, provider
    )


# 对外合成一个 router（/api/v1 + /webhook），app.py 只 include 这一个。
router = APIRouter()
router.include_router(api_router)
router.include_router(webhook_router)
