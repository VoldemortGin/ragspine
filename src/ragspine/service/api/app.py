"""FastAPI app factory：装配 config/provider/queue/faq_cache 到 app.state。

测试可注入临时 config + mock provider + fake queue + fake FAQ cache；
生产由 from_env + 默认实例装配。HTTP 层只做边界适配，不重写业务 workflow。
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.api.dify_public import router as dify_public_router
from ragspine.service.api.routes import router
from ragspine.service.config import ServiceConfig, build_provider
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.n8n_public.router import router as n8n_public_router
from ragspine.service.studio.launch import LaunchSessionRegistry
from ragspine.service.tasks.task_queue import RQQueue, TaskQueue
from ragspine.workflows.matching import TemplateMatcher, make_template_matcher

_WORKFLOW_JSON_PATHS = frozenset(
    {
        "/v1/workflow-package",
        "/v1/workflow-readiness",
        "/v1/workflow-scaffold",
        "/v1/dify/analyze",
        "/v1/dify/compile",
        "/v1/dify/run",
        "/v1/dify/run/jobs",
    }
)
_MAX_WORKFLOW_REQUEST_BYTES = 2 * 1024 * 1024


class _BoundedWorkflowRequestBody:
    """Bound selected JSON ingress before Starlette/Pydantic buffers the body."""

    def __init__(self, app: ASGIApp, *, limit: int = _MAX_WORKFLOW_REQUEST_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _WORKFLOW_JSON_PATHS
        ):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", ()):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except (TypeError, ValueError):
                continue
            if declared > self.limit:
                await self._reject(scope, receive, send)
                return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                messages.append(message)
                break
            body = message.get("body", b"")
            total += len(body)
            if total > self.limit:
                await self._reject(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "type": "RequestTooLarge",
                    "message": "request body too large",
                    "request_id": None,
                }
            },
        )
        await response(scope, receive, send)


async def _request_validation_error_response(
    request: Request,
    exc: Exception,
) -> Response:
    """Never reflect rejected request values (which may be credentials) in 422 bodies."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    del request

    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        # FastAPI 默认把 Pydantic error.input 原样放进 422；若额外字段是 API key，
        # 会在“已拒绝”后仍把密钥反射回响应。ctx 也可能携带带值的异常，二者都删除。
        errors.append({key: value for key, value in error.items() if key not in {"input", "ctx"}})
    return JSONResponse(status_code=422, content={"detail": errors})


def create_app(
    config: ServiceConfig | None = None,
    *,
    provider: LLMProvider | None = None,
    queue: TaskQueue | None = None,
    faq_cache: FAQCache | None = None,
    workflow_matcher: TemplateMatcher | None = None,
    launch_sessions: LaunchSessionRegistry | None = None,
) -> FastAPI:
    config = config or ServiceConfig.from_env()
    provider = provider or build_provider(config)
    if faq_cache is None:
        faq_cache = FAQCache.from_file(config.faq_source) if config.faq_source else FAQCache.empty()
    queue = queue or RQQueue(config.redis_url)
    if workflow_matcher is None:
        workflow_matcher = make_template_matcher(config.workflow_matcher)
    if launch_sessions is None:
        launch_sessions = LaunchSessionRegistry()

    app = FastAPI(title="RAGSpine Service")
    app.add_middleware(_BoundedWorkflowRequestBody)
    app.add_exception_handler(RequestValidationError, _request_validation_error_response)
    app.state.config = config
    app.state.provider = provider
    app.state.queue = queue
    app.state.faq_cache = faq_cache
    app.state.workflow_matcher = workflow_matcher
    app.state.launch_sessions = launch_sessions
    app.include_router(router)
    app.include_router(dify_public_router)  # dify 官方 Workflow API 形状克隆（/v1/workflows/*）
    app.include_router(n8n_public_router)  # n8n 官方 Public API 形状克隆（/api/v1/* + /webhook/*）
    # Studio 前端静态站点：默认目录是 wheel 内 ragspine.service 包下的 studio_dist；环境变量
    # RAGSPINE_STUDIO_DIR 可覆盖，显式空串可禁用。目录缺失时 /studio 为 404，API 不受影响。
    if config.studio_dir and Path(config.studio_dir).is_dir():
        app.mount("/studio", StaticFiles(directory=config.studio_dir, html=True), name="studio")
    return app
