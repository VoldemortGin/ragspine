"""Standalone ASGI boundary copied beside the unchanged official WebUI app.

This file deliberately imports no project module so the vendor container does
not need the project's Python environment. It is exercised by the offline gate.
"""

import argparse
import json
import os
import re
import secrets
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast

import httpx
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AIA_REVIEW_MODEL = "aia-2026-interim-source-review-v1"
# The document-catalog profile serves one model per mounted document (``rag-chat-v1``); the
# gate itself relays ``/v1/models`` and ``/v1/chat/completions`` to the backend.
DOCUMENT_CATALOG_PROFILE = "document-catalog"
DOCUMENT_MODEL_PREFIX = "enterprise-pdf-rag/"


def source_review_page(value: str) -> int | None:
    question = normalize_demo_question(value)
    if question in {
        "查看当前文件",
        "当前文件",
        "这是什么文件",
        "文件状态",
        "show source document",
        "show aia source",
        "查看来源",
    }:
        return 0
    match = re.fullmatch(r"(?:查看|展示)?第?\s*(\d{1,3})\s*页(?:原文|文本|内容)?", question)
    if match is None:
        match = re.fullmatch(r"(?:show )?page (\d{1,3})(?: text)?", question)
    return int(match.group(1)) if match is not None and int(match.group(1)) > 0 else None


DEMO_QUESTIONS = frozenset(
    {
        "show demo chart evidence",
        "demo evidence",
        "revenue",
        "revenue 2024",
        "revenue 2025",
        "revenue 2024 2025",
        "展示演示图表证据",
        "演示图表",
    }
)
LOCAL_PLACEHOLDER_KEY = "local-offline-demo-no-upstream-key"
_READ_PATHS = frozenset(
    {
        "/api/config",
        "/api/version",
        "/api/models",
        "/api/models/base",
        "/api/v1/auths",
        "/api/v1/users/user/settings",
        "/api/v1/users/user/info",
        "/api/v1/users/user/permissions",
        "/api/v1/configs/banners",
        "/api/v1/configs/prompts/suggestions",
        "/api/v1/models",
        "/api/v1/folders",
    }
)
# Read-only capability discovery must not initialize vendor tool servers.
_DISABLED_READS = {
    "/api/v1/tools": b"[]",
    "/api/v1/channels": b"[]",
    "/api/changelog": b"{}",
}
_CHAT_PATHS = frozenset({"/api/chat/completions", "/api/chat/completed"})
_RELAY_ROUTES = {"/v1/models": "GET", "/v1/chat/completions": "POST"}
# Only these backend headers are repeated; the server name and framing are this process's.
_RELAY_HEADERS = frozenset(
    {"content-type", "content-encoding", "cache-control", "x-accel-buffering"}
)
_DOCUMENT_ROLES = frozenset({"system", "user", "assistant"})
_DOCUMENT_REFERENCE = re.compile(r"[0-9a-f]{12,64}")
_LOCAL_BACKEND = re.compile(r"http://(?:127\.0\.0\.1|api):\d{1,5}/v1")
_FORBIDDEN_INPUTS = frozenset(
    {
        "files",
        "file_ids",
        "file_id",
        "tools",
        "tool_ids",
        "tool_id",
        "tool_servers",
        "knowledge",
        "collection_names",
        "collection_name",
        "urls",
        "url",
        "documents",
        "data_sources",
        "function_call",
        "functions",
    }
)


def normalize_demo_question(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()).rstrip(".。?!")


def _allowed_route(path: str, method: str) -> bool:
    if "//" in path or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        return False
    normalized = path.rstrip("/") or "/"
    if normalized == "/ws/socket.io":
        return method in {"GET", "POST"}
    if method in {"GET", "HEAD"}:
        if (
            normalized in _READ_PATHS
            or normalized.startswith("/api/v1/chats/")
            or normalized == "/api/v1/chats"
        ):
            return True
        return not normalized.startswith(
            ("/api", "/openai", "/ollama", "/anthropic", "/oauth", "/ws")
        )
    if method == "POST":
        return (
            normalized
            in _CHAT_PATHS | {"/api/v1/auths/signin", "/api/v1/auths/signout", "/api/v1/chats/new"}
            or re.fullmatch(r"/api/v1/chats/[0-9a-f-]{36}", normalized) is not None
        )
    return False


async def _reject(
    send: Send,
    status: int = 403,
    detail: str = "This local demo disables uploads, built-in RAG, tools and configuration changes.",
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()})


async def _read_body(receive: Receive, send: Send) -> bytes | None:
    """The whole request body, or ``None`` once the client left or a 413 was answered."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        body.extend(message.get("body", b""))
        if len(body) > 524288:
            await _reject(send, 413)
            return None
        if not message.get("more_body", False):
            return bytes(body)


def _model_accepted(model: object, model_id: str) -> bool:
    if model_id == DOCUMENT_CATALOG_PROFILE:
        return isinstance(model, str) and model.startswith(DOCUMENT_MODEL_PREFIX)
    return model == model_id


def _question_accepted(content: str, model_id: str) -> bool:
    if model_id == DOCUMENT_CATALOG_PROFILE:
        # Free text: the backend answers only from verified evidence or abstains.
        return True
    question = normalize_demo_question(content)
    if model_id == AIA_REVIEW_MODEL:
        return source_review_page(question) is not None
    return question in DEMO_QUESTIONS


def _safe_chat(payload: object, model_id: str) -> bool:
    if not isinstance(payload, dict) or not _model_accepted(payload.get("model"), model_id):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        return False
    has_user = False
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
        ):
            return False
        if message["role"] == "user":
            has_user = True
            if not _question_accepted(message["content"], model_id):
                return False
    if not has_user:
        return False
    pending: list[object] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _FORBIDDEN_INPUTS and item not in (None, [], {}, False):
                    return False
                if key in {"features", "background_tasks"} and (
                    not isinstance(item, dict)
                    or any(flag is not False and flag is not None for flag in item.values())
                ):
                    return False
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return True


def _plain_text(content: object) -> str | None:
    """Text content as one string; ``None`` for images or any other non-text part.

    Images are refused rather than silently dropped: an answer that ignored an attached
    picture would look like it had been considered.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if (
            not isinstance(part, dict)
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
        ):
            return None
        parts.append(part["text"])
    return "\n".join(parts)


def document_chat_payload(payload: object) -> dict[str, object] | None:
    """The ``rag-chat-v1`` subset of a vendor chat request, or ``None`` when unservable.

    The UI adds bookkeeping (``metadata``, ``chat_id``, ``params``, sampling settings …)
    that the ``extra="forbid"`` backend would reject; it is dropped here instead. Kept:
    ``model`` (a document model), ``messages`` (role + plain text), ``stream``,
    ``stream_options.include_usage``, ``document`` and ``rerank`` when well-typed.
    """
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if not isinstance(model, str) or not model.startswith(DOCUMENT_MODEL_PREFIX):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        return None
    kept: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in _DOCUMENT_ROLES:
            return None
        text = _plain_text(message.get("content"))
        if text is None:
            return None
        kept.append({"role": message["role"], "content": text})
    clean: dict[str, object] = {"model": model, "messages": kept}
    if isinstance(payload.get("stream"), bool):
        clean["stream"] = payload["stream"]
    options = payload.get("stream_options")
    if isinstance(options, dict) and isinstance(options.get("include_usage"), bool):
        clean["stream_options"] = {"include_usage": options["include_usage"]}
    document = payload.get("document")
    if isinstance(document, str) and _DOCUMENT_REFERENCE.fullmatch(document):
        clean["document"] = document
    if payload.get("rerank") is True:
        clean["rerank"] = True
    return clean


def _visible_error(body: bytes) -> bytes:
    """Repeat a FastAPI ``detail`` under ``error.message``, the only key the UI displays."""
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return body
    if (
        not isinstance(parsed, dict)
        or "error" in parsed
        or not isinstance(parsed.get("detail"), str)
    ):
        return body
    return json.dumps({**parsed, "error": {"message": parsed["detail"]}}).encode()


class DocumentRelay:
    """Serves the two OpenAI routes the UI needs from the document-catalog backend.

    Only ``document_chat_payload`` leaves this process; the UI's placeholder credential is
    checked and dropped, so no ``Authorization`` header reaches the backend, which needs
    none. Streamed bodies are repeated chunk by chunk, never buffered.
    """

    def __init__(
        self, backend_url: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = backend_url.rstrip("/")
        self._transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"].rstrip("/")
        expected = f"Bearer {LOCAL_PLACEHOLDER_KEY}".encode()
        if (
            scope["method"] != _RELAY_ROUTES[path]
            or dict(scope["headers"]).get(b"authorization") != expected
        ):
            await _reject(send, detail="Only the local Open WebUI may use the document relay.")
            return
        if path == "/v1/models":
            await self._forward("GET", "/models", None, send)
            return
        body = await _read_body(receive, send)
        if body is None:
            return
        try:
            payload: object = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
        clean = document_chat_payload(payload)
        if clean is None:
            await _reject(
                send,
                detail="Only text questions to an enterprise-pdf-rag/<document> model are relayed.",
            )
            return
        await self._forward("POST", "/chat/completions", json.dumps(clean).encode(), send)

    async def _forward(self, method: str, route: str, body: bytes | None, send: Send) -> None:
        headers = {"accept": "application/json, text/event-stream"}
        if body is not None:
            headers["content-type"] = "application/json"
        started = False
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=httpx.Timeout(600.0, connect=5.0),
                    trust_env=False,
                ) as client,
                client.stream(
                    method, self._base + route, content=body, headers=headers
                ) as upstream,
            ):
                streamed = upstream.headers.get("content-type", "").startswith("text/event-stream")
                await send(
                    {
                        "type": "http.response.start",
                        "status": upstream.status_code,
                        "headers": [
                            (key.encode(), value.encode())
                            for key, value in upstream.headers.multi_items()
                            if key.lower() in _RELAY_HEADERS
                        ],
                    }
                )
                started = True
                if upstream.is_success or streamed:
                    async for chunk in upstream.aiter_raw():
                        if chunk:
                            await send(
                                {"type": "http.response.body", "body": chunk, "more_body": True}
                            )
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": _visible_error(await upstream.aread()),
                        }
                    )
        except httpx.HTTPError:
            if not started:
                await _reject(send, 503, "Document backend is unavailable; no substitute.")
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})


class WebUIBoundary:
    def __init__(
        self,
        app: ASGIApp,
        *,
        model_id: str = "enterprise-pdf-rag-offline-demo-v1",
        relay: DocumentRelay | None = None,
        login: bool = False,
    ) -> None:
        if model_id not in {
            AIA_REVIEW_MODEL,
            "enterprise-pdf-rag-offline-demo-v1",
            DOCUMENT_CATALOG_PROFILE,
        }:
            raise ValueError("Unknown local UI profile")
        if (relay is None) == (model_id == DOCUMENT_CATALOG_PROFILE):
            raise ValueError("A document relay belongs to exactly the document-catalog profile")
        if login and model_id != DOCUMENT_CATALOG_PROFILE:
            raise ValueError("The vendor login is an option of the document-catalog profile only")
        self._app = app
        self._model_id = model_id
        self._relay = relay
        # With the vendor's own login the first sign-up creates the admin account.
        self._login = login

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._app(scope, receive, send)
            return
        if kind == "websocket":
            if scope["path"].rstrip("/") != "/ws/socket.io":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._app(scope, receive, send)
            return
        if self._relay is not None and scope["path"].rstrip("/") in _RELAY_ROUTES:
            await self._relay(scope, receive, send)
            return
        disabled = _DISABLED_READS.get(scope["path"].rstrip("/"))
        if scope["method"] == "GET" and disabled is not None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": disabled})
            return
        signup = self._login and scope["path"].rstrip("/") == "/api/v1/auths/signup"
        if not (signup and scope["method"] == "POST") and not _allowed_route(
            scope["path"], scope["method"]
        ):
            await _reject(send)
            return
        if scope["method"] == "POST" and scope["path"].rstrip("/") in _CHAT_PATHS:
            raw = await _read_body(receive, send)
            if raw is None:
                return
            body = bytearray(raw)
            try:
                payload: object = json.loads(body)
            except (ValueError, RecursionError):
                await _reject(send)
                return
            # The 0.6.5 UI asks for title/tags by default even when server flags
            # disable them. Turn only these known bookkeeping tasks off before
            # any upstream middleware; unknown enabled tasks still fail closed.
            if isinstance(payload, dict):
                tasks = payload.get("background_tasks")
                if (
                    isinstance(tasks, dict)
                    and tasks.keys() <= {"title_generation", "tags_generation"}
                    and all(isinstance(flag, bool) for flag in tasks.values())
                ):
                    payload["background_tasks"] = dict.fromkeys(tasks, False)
                    body = bytearray(json.dumps(payload).encode())
                    scope = {
                        **scope,
                        "headers": [
                            (key, value)
                            for key, value in scope["headers"]
                            if key.lower() != b"content-length"
                        ]
                        + [(b"content-length", str(len(body)).encode())],
                    }
            if not _safe_chat(payload, self._model_id):
                await _reject(send)
                return
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            await self._app(scope, replay, send)
            return
        await self._app(scope, receive, send)


def installed_webui_version() -> str:
    try:
        return version("open-webui")
    except PackageNotFoundError:
        # The official source-based container has package.json, not pip metadata.
        metadata: dict[str, object] = json.loads(Path("/app/package.json").read_text())
        result = metadata.get("version")
        if not isinstance(result, str):
            raise RuntimeError("Missing official Open WebUI release metadata") from None
        return result


def create_guarded_app() -> ASGIApp:
    expected = os.environ.get("ENTERPRISE_WEBUI_VERSION", "0.11.3")
    if expected not in {"0.11.3", "0.6.5"} or installed_webui_version() != expected:
        raise RuntimeError(
            "Open WebUI version differs from the pinned target or explicit compatibility preview"
        )
    if (
        os.environ.get("OPENAI_API_KEY") != LOCAL_PLACEHOLDER_KEY
        or os.environ.get("OPENAI_API_KEYS") != LOCAL_PLACEHOLDER_KEY
        or "OPENAI_BASE_URL" in os.environ
    ):
        raise RuntimeError(
            "Open WebUI must receive only the local placeholder credential in an isolated environment"
        )
    backend = os.environ.get("ENTERPRISE_WEBUI_BACKEND_URL", "http://127.0.0.1:8766/v1")
    if _LOCAL_BACKEND.fullmatch(backend) is None:
        raise RuntimeError("Open WebUI may connect only to the local demo backend")
    profile = os.environ.get("ENTERPRISE_WEBUI_PROFILE", "")
    if profile == DOCUMENT_CATALOG_PROFILE:
        # The UI talks to this process's own relay; only the relay reaches the backend.
        allowed = {os.environ.get("WEBUI_URL", "") + "/v1"}
    else:
        allowed = {backend, "http://api:8766/v1", "http://127.0.0.1:8766/v1"}
    if (
        os.environ.get("OPENAI_API_BASE_URL") not in allowed
        or os.environ.get("OPENAI_API_BASE_URLS") not in allowed
    ):
        raise RuntimeError("Open WebUI may connect only to the local demo backend")
    vendor = cast(ASGIApp, import_module("open_webui.main").app)
    if profile == DOCUMENT_CATALOG_PROFILE:
        return WebUIBoundary(
            vendor,
            model_id=profile,
            relay=DocumentRelay(backend),
            login=os.environ.get("WEBUI_AUTH") == "True",
        )
    return WebUIBoundary(vendor, model_id=os.environ["DEFAULT_MODELS"])


@dataclass(frozen=True)
class LoginOptions:
    """The vendor's own login, opted in explicitly; the first sign-up becomes the admin."""

    signup: bool = True
    default_role: str = "pending"


def login_options(environment: Mapping[str, str]) -> LoginOptions | None:
    """``ENTERPRISE_WEBUI_AUTH=1`` opts in; ``ENABLE_SIGNUP`` / ``DEFAULT_USER_ROLE`` then pass.

    ``ENABLE_SIGNUP`` is parsed exactly as Open WebUI parses it (``.lower() == "true"``).
    """
    if environment.get("ENTERPRISE_WEBUI_AUTH") != "1":
        return None
    role = environment.get("DEFAULT_USER_ROLE", "pending")
    if role not in {"pending", "user", "admin"}:
        raise ValueError("DEFAULT_USER_ROLE must be pending, user or admin")
    return LoginOptions(
        signup=environment.get("ENABLE_SIGNUP", "True").lower() == "true", default_role=role
    )


def build_webui_environment(
    *,
    data_dir: Path,
    secret: str,
    preview: bool = False,
    profile: str = "aia-source-review",
    ui_port: int = 8767,
    backend_port: int = 8766,
    login: LoginOptions | None = None,
) -> dict[str, str]:
    """Construct, rather than inherit, the entire vendor process environment."""
    if profile not in {"aia-source-review", "offline-demo", DOCUMENT_CATALOG_PROFILE}:
        raise ValueError("Unknown local UI profile")
    if login is not None and profile != DOCUMENT_CATALOG_PROFILE:
        raise ValueError("The vendor login is an option of the document-catalog profile only")
    data_dir = data_dir.resolve()
    backend = f"http://{'127.0.0.1' if preview else 'api'}:{backend_port}/v1"
    # The catalog profile points the vendor at this process's relay, never at the backend.
    endpoint = f"http://127.0.0.1:{ui_port}/v1" if profile == DOCUMENT_CATALOG_PROFILE else backend
    names = {
        "aia-source-review": "AIA Source Review",
        "offline-demo": "PDF RAG Offline Demo",
        DOCUMENT_CATALOG_PROFILE: "PDF RAG Document Catalog",
    }
    environment = {
        "PATH": f"{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "ENTERPRISE_WEBUI_VERSION": "0.6.5" if preview else "0.11.3",
        "ENV": "prod",
        "DATA_DIR": str(data_dir),
        "STATIC_DIR": str(data_dir / "static"),
        "FONTS_DIR": str(data_dir / "static" / "fonts"),
        "USER_AGENT": "enterprise-pdf-rag-offline-demo",
        "DATABASE_URL": f"sqlite:///{data_dir}/webui.db",
        "WEBUI_SECRET_KEY": secret,
        "WEBUI_AUTH": "False",
        "WEBUI_URL": f"http://127.0.0.1:{ui_port}",
        "WEBUI_NAME": names[profile] + (" (0.6.5 preview)" if preview else ""),
        "CORS_ALLOW_ORIGIN": f"http://127.0.0.1:{ui_port};http://localhost:{ui_port}",
        "ENABLE_PERSISTENT_CONFIG": "False",
        "RESET_CONFIG_ON_START": "True",
        "ENABLE_OPENAI_API": "True",
        "OPENAI_API_BASE_URL": endpoint,
        "OPENAI_API_BASE_URLS": endpoint,
        "OPENAI_API_KEY": LOCAL_PLACEHOLDER_KEY,
        "OPENAI_API_KEYS": LOCAL_PLACEHOLDER_KEY,
        "OFFLINE_MODE": "True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(data_dir / "cache" / "huggingface"),
        "TORCH_HOME": str(data_dir / "cache" / "torch"),
        "XDG_CACHE_HOME": str(data_dir / "cache"),
        "TIKTOKEN_CACHE_DIR": str(data_dir / "cache" / "tiktoken"),
        "RAG_EMBEDDING_ENGINE": "openai",
        "RAG_EMBEDDING_MODEL": "disabled-offline-demo",
        "RAG_OPENAI_API_BASE_URL": endpoint,
        "RAG_OPENAI_API_KEY": LOCAL_PLACEHOLDER_KEY,
        "RAG_RERANKING_MODEL": "",
        "SAFE_MODE": "True",
        "DO_NOT_TRACK": "True",
        "SCARF_NO_ANALYTICS": "True",
        "ANONYMIZED_TELEMETRY": "False",
    }
    disabled = (
        "ENABLE_SIGNUP",
        "ENABLE_OLLAMA_API",
        "ENABLE_DIRECT_CONNECTIONS",
        "ENABLE_TITLE_GENERATION",
        "ENABLE_TAGS_GENERATION",
        "ENABLE_FOLLOW_UP_GENERATION",
        "ENABLE_SEARCH_QUERY_GENERATION",
        "ENABLE_RETRIEVAL_QUERY_GENERATION",
        "ENABLE_AUTOCOMPLETE_GENERATION",
        "ENABLE_WEB_SEARCH",
        "ENABLE_CODE_EXECUTION",
        "ENABLE_CODE_INTERPRETER",
        "ENABLE_IMAGE_GENERATION",
        "ENABLE_PLUGINS",
        "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS",
        "RAG_EMBEDDING_MODEL_AUTO_UPDATE",
        "RAG_RERANKING_MODEL_AUTO_UPDATE",
        "USER_PERMISSIONS_CHAT_FILE_UPLOAD",
        "USER_PERMISSIONS_CHAT_WEB_UPLOAD",
        "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS",
    )
    environment.update(dict.fromkeys(disabled, "False"))
    if profile == DOCUMENT_CATALOG_PROFILE:
        # One model per mounted document: the UI lists them; nothing is preselected.
        environment["ENTERPRISE_WEBUI_PROFILE"] = profile
        environment["ENTERPRISE_WEBUI_BACKEND_URL"] = backend
        if login is not None:
            environment["WEBUI_AUTH"] = "True"
            environment["ENABLE_SIGNUP"] = "True" if login.signup else "False"
            environment["DEFAULT_USER_ROLE"] = login.default_role
    else:
        environment["DEFAULT_MODELS"] = (
            AIA_REVIEW_MODEL
            if profile == "aia-source-review"
            else "enterprise-pdf-rag-offline-demo-v1"
        )
    if preview:
        environment["FROM_INIT_PY"] = "True"
    return environment


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the isolated official Open WebUI demo")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--preview-legacy",
        action="store_true",
        help="Explicitly run the preinstalled 0.6.5 compatibility preview",
    )
    parser.add_argument(
        "--profile",
        choices=("aia-source-review", "offline-demo", DOCUMENT_CATALOG_PROFILE),
        default="aia-source-review",
    )
    parser.add_argument("--port", type=int, default=8767, help="This UI's listening port")
    parser.add_argument(
        "--backend-port", type=int, default=8766, help="Port of the local project API"
    )
    args = parser.parse_args()
    expected = "0.6.5" if args.preview_legacy else "0.11.3"
    if installed_webui_version() != expected:
        raise SystemExit(f"Expected Open WebUI {expected}; no global installation is performed")
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_file = data_dir / ".session-secret"
    if not secret_file.exists():
        with secret_file.open("x") as stream:
            stream.write(secrets.token_urlsafe(48))
        secret_file.chmod(0o600)
    environment = build_webui_environment(
        data_dir=data_dir,
        secret=secret_file.read_text(),
        preview=args.preview_legacy,
        profile=args.profile,
        ui_port=args.port,
        backend_port=args.backend_port,
        login=login_options(os.environ),
    )
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "webui_gate:create_guarded_app",
            "--factory",
            "--app-dir",
            str(Path(__file__).parent),
            "--host",
            "127.0.0.1" if args.preview_legacy else "0.0.0.0",
            "--port",
            str(args.port),
        ],
        environment,
    )


if __name__ == "__main__":
    main()
