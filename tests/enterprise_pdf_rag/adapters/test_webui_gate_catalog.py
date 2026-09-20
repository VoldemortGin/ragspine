"""The document-catalog profile relays only the ``rag-chat-v1`` subset, frame by frame."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from enterprise_pdf_rag.adapters.http.webui_gate import (
    DOCUMENT_CATALOG_PROFILE,
    LOCAL_PLACEHOLDER_KEY,
    DocumentRelay,
    LoginOptions,
    WebUIBoundary,
    build_webui_environment,
    login_options,
)

_MODEL = "enterprise-pdf-rag/0123456789ab"
_UI_HEADERS = {"Authorization": f"Bearer {LOCAL_PLACEHOLDER_KEY}"}
_BACKEND = "http://127.0.0.1:8768/v1"
_MODELS_BODY = {
    "object": "list",
    "data": [
        {
            "id": _MODEL,
            "name": "Annual report (0123456789ab)",
            "object": "model",
            "created": 0,
            "owned_by": "enterprise-pdf-rag/document-catalog",
        }
    ],
}


class Backend:
    """A stub document-catalog API that records exactly what the relay forwards."""

    def __init__(self, *, status: int = 200, body: object = None) -> None:
        self.status = status
        self.body = json.dumps({"ok": True} if body is None else body).encode()
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        self.requests.append((scope["method"], scope["path"], headers, bytes(body)))
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


async def _vendor_unreachable(_scope: Scope, _receive: Receive, _send: Send) -> None:
    raise AssertionError("The vendor app must not serve relay routes")


def _gate(backend: Backend, vendor: ASGIApp = _vendor_unreachable) -> WebUIBoundary:
    relay = DocumentRelay(_BACKEND, transport=httpx.ASGITransport(app=backend))
    return WebUIBoundary(vendor, model_id=DOCUMENT_CATALOG_PROFILE, relay=relay)


def _run(gate: ASGIApp, scenario: Callable[[httpx.AsyncClient], Awaitable[None]]) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gate), base_url="http://ui"
        ) as client:
            await scenario(client)

    asyncio.run(exercise())


def test_model_list_is_relayed_without_forwarding_the_placeholder_credential() -> None:
    backend = Backend(body=_MODELS_BODY)

    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/models", headers=_UI_HEADERS)
        assert response.status_code == 200
        assert response.json() == _MODELS_BODY
        assert response.headers["content-type"] == "application/json"
        [(method, path, headers, body)] = backend.requests
        assert (method, path, body) == ("GET", "/v1/models", b"")
        assert "authorization" not in headers
        assert not any(LOCAL_PLACEHOLDER_KEY in value for value in headers.values())

    _run(_gate(backend), scenario)


def test_relay_serves_only_the_co_located_ui() -> None:
    backend = Backend(body=_MODELS_BODY)

    async def scenario(client: httpx.AsyncClient) -> None:
        assert (await client.get("/v1/models")).status_code == 403
        anonymous = {"Authorization": "Bearer someone-else"}
        assert (await client.get("/v1/models", headers=anonymous)).status_code == 403
        assert (await client.delete("/v1/models", headers=_UI_HEADERS)).status_code == 403
        assert backend.requests == []

    _run(_gate(backend), scenario)


def test_ui_bookkeeping_fields_are_dropped_not_rejected() -> None:
    backend = Backend()
    sent = {
        "model": _MODEL,
        "messages": [
            {"role": "user", "content": "What does page 2 say?", "timestamp": 1},
            {"role": "assistant", "content": "Page 2 says…", "info": {"usage": {}}},
            {"role": "user", "content": "And page 3?"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True, "other": 1},
        "document": "0123456789abcdef",
        "rerank": True,
        "temperature": 0.2,
        "max_tokens": 512,
        "top_p": 0.9,
        "seed": 7,
        "stop": ["\n"],
        "params": {"temperature": 0.2},
        "metadata": {"chat_id": "c1", "message_id": "m1", "session_id": "s1"},
        "chat_id": "c1",
        "id": "m1",
        "session_id": "s1",
        "files": [],
        "tool_ids": [],
        "tool_servers": [],
        "features": {"web_search": False},
        "variables": {"{{USER_NAME}}": "u"},
        "model_item": {"id": _MODEL, "owned_by": "openai"},
        "background_tasks": {"title_generation": False},
        "user": "u1",
    }

    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/chat/completions", json=sent, headers=_UI_HEADERS)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        [(method, path, headers, body)] = backend.requests
        assert (method, path) == ("POST", "/v1/chat/completions")
        assert headers["content-type"] == "application/json"
        assert "authorization" not in headers
        assert json.loads(body) == {
            "model": _MODEL,
            "messages": [
                {"role": "user", "content": "What does page 2 say?"},
                {"role": "assistant", "content": "Page 2 says…"},
                {"role": "user", "content": "And page 3?"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "document": "0123456789abcdef",
            "rerank": True,
        }

    _run(_gate(backend), scenario)


def test_minimal_request_forwards_only_model_and_messages() -> None:
    backend = Backend()

    async def scenario(client: httpx.AsyncClient) -> None:
        sent = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "Q"}],
            "stream": "yes",
            "stream_options": "all",
            "document": "not-a-sha",
            "rerank": "true",
        }
        response = await client.post("/v1/chat/completions", json=sent, headers=_UI_HEADERS)
        assert response.status_code == 200
        [(_method, _path, _headers, body)] = backend.requests
        assert json.loads(body) == {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "Q"}],
        }

    _run(_gate(backend), scenario)


def test_text_parts_are_flattened_and_images_are_refused() -> None:
    backend = Backend()

    async def scenario(client: httpx.AsyncClient) -> None:
        parts = {
            "model": _MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First line"},
                        {"type": "text", "text": "Second line"},
                    ],
                }
            ],
        }
        response = await client.post("/v1/chat/completions", json=parts, headers=_UI_HEADERS)
        assert response.status_code == 200
        [(_method, _path, _headers, body)] = backend.requests
        assert json.loads(body)["messages"] == [
            {"role": "user", "content": "First line\nSecond line"}
        ]
        image = {
            "model": _MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    ],
                }
            ],
        }
        response = await client.post("/v1/chat/completions", json=image, headers=_UI_HEADERS)
        assert response.status_code == 403
        assert len(backend.requests) == 1

    _run(_gate(backend), scenario)


@pytest.mark.parametrize(
    "body",
    [
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "Q"}]},
        {
            "model": "aia-2026-interim-source-review-v1",
            "messages": [{"role": "user", "content": "查看第25页"}],
        },
        {"model": _MODEL, "messages": []},
        {"model": _MODEL, "messages": [{"role": "user", "content": "Q"}] * 33},
        {"model": _MODEL, "messages": [{"role": "tool", "content": "Q"}]},
        {"model": _MODEL, "messages": [{"role": "user"}]},
        {"model": _MODEL},
        [{"model": _MODEL}],
    ],
)
def test_requests_outside_the_contract_never_reach_the_backend(body: object) -> None:
    backend = Backend()

    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/chat/completions", json=body, headers=_UI_HEADERS)
        assert response.status_code == 403
        assert backend.requests == []

    _run(_gate(backend), scenario)


class _LazyFrames(httpx.AsyncByteStream):
    """Yields each SSE frame only after the gate has already sent the previous one."""

    def __init__(self, frames: list[bytes], sent: list[Message]) -> None:
        self._frames = frames
        self._sent = sent

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, frame in enumerate(self._frames):
            bodies = [m["body"] for m in self._sent if m["type"] == "http.response.body"]
            assert bodies == self._frames[:index], "the gate buffered instead of relaying"
            yield frame


class _StreamingBackend(httpx.AsyncBaseTransport):
    def __init__(self, frames: list[bytes], sent: list[Message]) -> None:
        self._frames = frames
        self._sent = sent
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream; charset=utf-8",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
                "server": "uvicorn",
            },
            stream=_LazyFrames(self._frames, self._sent),
        )


def test_sse_frames_are_relayed_as_they_arrive() -> None:
    frames = [
        b'data: {"choices":[{"delta":{"role":"assistant","content":"p.2"}}]}\n\n',
        b'data: {"choices":[],"enterprise_pdf_rag":{"status":"answered"}}\n\n',
        b"data: [DONE]\n\n",
    ]
    sent: list[Message] = []
    transport = _StreamingBackend(frames, sent)
    gate = WebUIBoundary(
        _vendor_unreachable,
        model_id=DOCUMENT_CATALOG_PROFILE,
        relay=DocumentRelay(_BACKEND, transport=transport),
    )
    request = json.dumps(
        {"model": _MODEL, "messages": [{"role": "user", "content": "Q"}], "stream": True}
    ).encode()
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {LOCAL_PLACEHOLDER_KEY}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(request)).encode()),
        ],
        "client": ("127.0.0.1", 40000),
        "server": ("127.0.0.1", 8769),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": request, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(gate(scope, receive, send))
    assert transport.bodies == [
        json.dumps(
            {"model": _MODEL, "messages": [{"role": "user", "content": "Q"}], "stream": True}
        ).encode()
    ]
    start, *bodies = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
    assert headers[b"cache-control"] == b"no-cache"
    assert b"server" not in headers
    assert b"content-length" not in headers
    assert [m["body"] for m in bodies] == [*frames, b""]
    assert [m.get("more_body", False) for m in bodies] == [True, True, True, False]


@pytest.mark.parametrize("status", [404, 409, 422, 503])
def test_backend_refusals_keep_their_status_and_become_visible_to_the_ui(status: int) -> None:
    backend = Backend(status=status, body={"detail": "Document is not mounted: not_indexed"})

    async def scenario(client: httpx.AsyncClient) -> None:
        body = {"model": _MODEL, "messages": [{"role": "user", "content": "Q"}]}
        response = await client.post("/v1/chat/completions", json=body, headers=_UI_HEADERS)
        assert response.status_code == status
        # Open WebUI surfaces only ``error.message``; the original ``detail`` stays beside it.
        assert response.json() == {
            "detail": "Document is not mounted: not_indexed",
            "error": {"message": "Document is not mounted: not_indexed"},
        }

    _run(_gate(backend), scenario)


def test_unreachable_backend_is_a_503_without_its_address() -> None:
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    gate = WebUIBoundary(
        _vendor_unreachable,
        model_id=DOCUMENT_CATALOG_PROFILE,
        relay=DocumentRelay(_BACKEND, transport=Down()),
    )

    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/models", headers=_UI_HEADERS)
        assert response.status_code == 503
        assert "8768" not in response.text
        assert "127.0.0.1" not in response.text

    _run(gate, scenario)


def test_ui_chat_accepts_any_document_model_and_free_text_but_no_uploads() -> None:
    received: list[str] = []

    async def vendor(_scope: Scope, receive: Receive, send: Send) -> None:
        payload = json.loads((await receive())["body"])
        received.append(payload["model"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    backend = Backend()

    async def scenario(client: httpx.AsyncClient) -> None:
        body = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "分析第 2 页的展望"}],
            "chat_id": "c1",
            "background_tasks": {"title_generation": False, "tags_generation": False},
        }
        assert (await client.post("/api/chat/completions", json=body)).status_code == 200
        other = {**body, "model": "enterprise-pdf-rag/fedcba987654"}
        assert (await client.post("/api/chat/completions", json=other)).status_code == 200
        foreign = {**body, "model": "enterprise-pdf-rag-offline-demo-v1"}
        assert (await client.post("/api/chat/completions", json=foreign)).status_code == 403
        upload = {**body, "files": [{"id": "a-pdf"}]}
        assert (await client.post("/api/chat/completions", json=upload)).status_code == 403
        assert received == [_MODEL, "enterprise-pdf-rag/fedcba987654"]
        assert backend.requests == []

    _run(_gate(backend, vendor), scenario)


def test_fixed_profiles_still_hand_openai_routes_to_the_vendor() -> None:
    calls: list[str] = []

    async def vendor(scope: Scope, _receive: Receive, send: Send) -> None:
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def scenario(client: httpx.AsyncClient) -> None:
        assert (await client.get("/v1/models", headers=_UI_HEADERS)).status_code == 404
        assert calls == ["/v1/models"]

    _run(WebUIBoundary(vendor), scenario)
    with pytest.raises(ValueError, match="relay"):
        WebUIBoundary(_vendor_unreachable, model_id=DOCUMENT_CATALOG_PROFILE)
    with pytest.raises(ValueError, match="profile"):
        WebUIBoundary(
            _vendor_unreachable,
            relay=DocumentRelay(_BACKEND, transport=httpx.ASGITransport(app=Backend())),
        )


def test_guarded_app_selects_the_relay_from_the_constructed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from enterprise_pdf_rag.adapters.http import webui_gate

    calls: list[str] = []

    async def vendor(scope: Scope, _receive: Receive, send: Send) -> None:
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(webui_gate, "installed_webui_version", lambda: "0.11.3")
    monkeypatch.setattr(webui_gate, "import_module", lambda _name: SimpleNamespace(app=vendor))
    for name in list(os.environ):
        if name.startswith(("OPENAI_", "ENTERPRISE_", "WEBUI_", "DEFAULT_MODELS")):
            monkeypatch.delenv(name)
    environment = build_webui_environment(
        data_dir=tmp_path, secret="s", profile=DOCUMENT_CATALOG_PROFILE, ui_port=3200
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    app = webui_gate.create_guarded_app()

    async def scenario(client: httpx.AsyncClient) -> None:
        # The relay answers before the vendor; without the placeholder it refuses.
        assert (await client.get("/v1/models")).status_code == 403
        assert calls == []
        assert (await client.get("/api/config")).status_code == 404
        assert calls == ["/api/config"]

    _run(app, scenario)
    monkeypatch.setenv("OPENAI_API_BASE_URL", "http://127.0.0.1:8766/v1")
    monkeypatch.setenv("OPENAI_API_BASE_URLS", "http://127.0.0.1:8766/v1")
    with pytest.raises(RuntimeError, match="local demo backend"):
        webui_gate.create_guarded_app()
    monkeypatch.setenv("OPENAI_API_BASE_URL", environment["OPENAI_API_BASE_URL"])
    monkeypatch.setenv("OPENAI_API_BASE_URLS", environment["OPENAI_API_BASE_URLS"])
    monkeypatch.setenv("ENTERPRISE_WEBUI_BACKEND_URL", "http://backend.example:8766/v1")
    with pytest.raises(RuntimeError, match="local demo backend"):
        webui_gate.create_guarded_app()
    monkeypatch.setenv("ENTERPRISE_WEBUI_BACKEND_URL", environment["ENTERPRISE_WEBUI_BACKEND_URL"])
    monkeypatch.setenv("WEBUI_AUTH", "True")
    calls.clear()

    async def login(client: httpx.AsyncClient) -> None:
        form = {"name": "linhan", "email": "linhan@local.test", "password": "pw"}
        assert (await client.post("/api/v1/auths/signup", json=form)).status_code == 404
        assert calls == ["/api/v1/auths/signup"]

    _run(webui_gate.create_guarded_app(), login)


def test_vendor_login_stays_off_unless_explicitly_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTERPRISE_WEBUI_AUTH", raising=False)
    monkeypatch.setenv("ENABLE_SIGNUP", "True")
    monkeypatch.setenv("DEFAULT_USER_ROLE", "admin")
    assert login_options(os.environ) is None
    catalog = build_webui_environment(
        data_dir=tmp_path, secret="s", profile=DOCUMENT_CATALOG_PROFILE
    )
    assert catalog["WEBUI_AUTH"] == "False"
    assert catalog["ENABLE_SIGNUP"] == "False"
    assert "DEFAULT_USER_ROLE" not in catalog
    legacy = build_webui_environment(data_dir=tmp_path, secret="s", preview=True)
    assert legacy["WEBUI_AUTH"] == "False"
    assert legacy["ENABLE_SIGNUP"] == "False"
    with pytest.raises(ValueError, match="document-catalog"):
        build_webui_environment(data_dir=tmp_path, secret="s", login=LoginOptions())


def test_login_opt_in_enables_vendor_auth_without_any_model_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "upstream-secret")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    monkeypatch.setenv("ENTERPRISE_WEBUI_AUTH", "1")
    monkeypatch.delenv("ENABLE_SIGNUP", raising=False)
    monkeypatch.delenv("DEFAULT_USER_ROLE", raising=False)
    options = login_options(os.environ)
    assert options == LoginOptions(signup=True, default_role="pending")
    environment = build_webui_environment(
        data_dir=tmp_path, secret="s", profile=DOCUMENT_CATALOG_PROFILE, login=options
    )
    # Exactly what Open WebUI 0.6.5 parses: ``.lower() == "true"`` and a role name.
    assert environment["WEBUI_AUTH"] == "True"
    assert environment["ENABLE_SIGNUP"] == "True"
    assert environment["DEFAULT_USER_ROLE"] == "pending"
    assert environment["OPENAI_API_KEY"] == LOCAL_PLACEHOLDER_KEY
    assert "upstream-secret" not in environment.values()
    assert "embedding-secret" not in environment.values()
    assert not any(name.startswith("EMBEDDING_") for name in environment)


def test_signup_and_default_role_pass_through_only_with_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTERPRISE_WEBUI_AUTH", "1")
    monkeypatch.setenv("ENABLE_SIGNUP", "false")
    monkeypatch.setenv("DEFAULT_USER_ROLE", "user")
    options = login_options(os.environ)
    assert options == LoginOptions(signup=False, default_role="user")
    environment = build_webui_environment(
        data_dir=tmp_path, secret="s", profile=DOCUMENT_CATALOG_PROFILE, login=options
    )
    assert environment["WEBUI_AUTH"] == "True"
    assert environment["ENABLE_SIGNUP"] == "False"
    assert environment["DEFAULT_USER_ROLE"] == "user"
    monkeypatch.setenv("DEFAULT_USER_ROLE", "root")
    with pytest.raises(ValueError, match="DEFAULT_USER_ROLE"):
        login_options(os.environ)
    monkeypatch.setenv("ENTERPRISE_WEBUI_AUTH", "true")
    assert login_options(os.environ) is None


def test_signup_route_reaches_the_vendor_only_when_login_is_on() -> None:
    calls: list[str] = []

    async def vendor(scope: Scope, _receive: Receive, send: Send) -> None:
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    form = {"name": "linhan", "email": "linhan@local.test", "password": "pw"}
    backend = Backend()
    relay = DocumentRelay(_BACKEND, transport=httpx.ASGITransport(app=backend))

    async def denied(client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/auths/signup", json=form)).status_code == 403
        assert calls == []

    _run(_gate(backend, vendor), denied)
    _run(WebUIBoundary(vendor), denied)
    with pytest.raises(ValueError, match="document-catalog"):
        WebUIBoundary(vendor, login=True)

    async def allowed(client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/auths/signup", json=form)).status_code == 200
        assert (await client.post("/api/v1/auths/signin", json=form)).status_code == 200
        assert calls == ["/api/v1/auths/signup", "/api/v1/auths/signin"]
        assert (await client.post("/api/v1/auths/add", json=form)).status_code == 403

    _run(
        WebUIBoundary(vendor, model_id=DOCUMENT_CATALOG_PROFILE, relay=relay, login=True),
        allowed,
    )


def test_catalog_environment_points_the_ui_at_its_own_relay(tmp_path: Path) -> None:
    environment = build_webui_environment(
        data_dir=tmp_path,
        secret="local-session-secret",
        preview=True,
        profile=DOCUMENT_CATALOG_PROFILE,
        ui_port=8769,
        backend_port=8768,
    )
    assert environment["ENTERPRISE_WEBUI_PROFILE"] == DOCUMENT_CATALOG_PROFILE
    assert environment["ENTERPRISE_WEBUI_BACKEND_URL"] == "http://127.0.0.1:8768/v1"
    assert environment["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8769/v1"
    assert environment["OPENAI_API_BASE_URLS"] == "http://127.0.0.1:8769/v1"
    assert environment["WEBUI_URL"] == "http://127.0.0.1:8769"
    assert environment["CORS_ALLOW_ORIGIN"] == "http://127.0.0.1:8769;http://localhost:8769"
    assert environment["OPENAI_API_KEY"] == LOCAL_PLACEHOLDER_KEY
    assert "DEFAULT_MODELS" not in environment
    assert "8766" not in "".join(environment.values())
    container = build_webui_environment(
        data_dir=tmp_path, secret="s", profile=DOCUMENT_CATALOG_PROFILE
    )
    assert container["ENTERPRISE_WEBUI_BACKEND_URL"] == "http://api:8766/v1"
    assert container["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8767/v1"
    legacy = build_webui_environment(data_dir=tmp_path, secret="s", preview=True)
    assert legacy["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8766/v1"
    assert legacy["DEFAULT_MODELS"] == "aia-2026-interim-source-review-v1"
    assert "ENTERPRISE_WEBUI_PROFILE" not in legacy
