"""Uploads and built-in retrieval are stopped before the vendor app runs."""

import asyncio
import json

import pytest
from httpx2 import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from enterprise_pdf_rag.adapters.http.webui_gate import WebUIBoundary


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/files/",
        "/api/v1/knowledge/create",
        "/api/v1/retrieval/process/file",
        "/api/v1/functions/create",
        "/openai/config/update",
    ],
)
def test_document_and_configuration_routes_never_reach_vendor(path: str) -> None:
    calls: list[str] = []

    async def vendor(scope: Scope, _receive: Receive, send: Send) -> None:
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"vendor-called"})

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor)), base_url="http://test"
        ) as client:
            response = await client.post(
                path,
                content=b"private PDF bytes",
                headers={"Authorization": "Bearer admin-token"},
            )
            assert response.status_code == 403
            assert calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "extra",
    [
        {"files": [{"id": "a-pdf"}]},
        {"features": {"web_search": True}},
        {"background_tasks": {"unknown_generation": True}},
        {"metadata": {"tool_ids": ["local-tool"]}},
        {"messages": [{"role": "user", "content": "#https://example.test/private.pdf"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AA"},
                        }
                    ],
                }
            ]
        },
    ],
)
def test_chat_cannot_smuggle_document_or_tool_processing(
    extra: dict[str, object],
) -> None:
    calls: list[bool] = []

    async def vendor(_scope: Scope, _receive: Receive, send: Send) -> None:
        calls.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"vendor-called"})

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor)), base_url="http://test"
        ) as client:
            body = {
                "model": "enterprise-pdf-rag-offline-demo-v1",
                "messages": [{"role": "user", "content": "Show demo chart evidence"}],
                **extra,
            }
            response = await client.post("/api/chat/completions", json=body)
            assert response.status_code == 403
            assert calls == []

    asyncio.run(exercise())


def test_safe_chat_body_is_replayed_to_the_real_vendor_app() -> None:
    received: list[object] = []

    async def vendor(_scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        received.append(json.loads(message["body"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor)), base_url="http://test"
        ) as client:
            body = {
                "model": "enterprise-pdf-rag-offline-demo-v1",
                "messages": [{"role": "user", "content": "Show demo chart evidence"}],
                "features": {"web_search": False},
                "files": [],
            }
            response = await client.post("/api/chat/completions", json=body)
            assert response.status_code == 200
            assert received == [body]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("path", "empty"),
    [("/api/v1/tools/", []), ("/api/v1/channels/", []), ("/api/changelog", {})],
)
def test_disabled_read_capabilities_return_empty_without_vendor_execution(
    path: str, empty: object
) -> None:
    calls: list[bool] = []

    async def vendor(_scope: Scope, _receive: Receive, _send: Send) -> None:
        calls.append(True)

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor)), base_url="http://test"
        ) as client:
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json() == empty
            assert calls == []

    asyncio.run(exercise())


def test_legacy_default_background_tasks_are_disabled_before_vendor() -> None:
    received: list[object] = []

    async def vendor(_scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        received.append(json.loads(message["body"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor)), base_url="http://test"
        ) as client:
            body = {
                "model": "enterprise-pdf-rag-offline-demo-v1",
                "messages": [{"role": "user", "content": "Show demo chart evidence"}],
                "background_tasks": {"title_generation": True, "tags_generation": True},
            }
            response = await client.post("/api/chat/completions", json=body)
            assert response.status_code == 200
            assert received == [
                {
                    **body,
                    "background_tasks": {
                        "title_generation": False,
                        "tags_generation": False,
                    },
                }
            ]

    asyncio.run(exercise())


def test_aia_profile_accepts_only_source_review_not_the_synthetic_model() -> None:
    from enterprise_pdf_rag.adapters.http.webui_gate import AIA_REVIEW_MODEL

    received: list[str] = []

    async def vendor(_scope: Scope, receive: Receive, send: Send) -> None:
        payload = json.loads((await receive())["body"])
        received.append(payload["messages"][0]["content"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=WebUIBoundary(vendor, model_id=AIA_REVIEW_MODEL)),
            base_url="http://test",
        ) as client:
            body = {
                "model": AIA_REVIEW_MODEL,
                "messages": [{"role": "user", "content": "查看第25页"}],
            }
            assert (await client.post("/api/chat/completions", json=body)).status_code == 200
            assert (
                await client.post(
                    "/api/chat/completions",
                    json={**body, "model": "enterprise-pdf-rag-offline-demo-v1"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/chat/completions",
                    json={
                        **body,
                        "messages": [{"role": "user", "content": "分析友邦利润增长"}],
                    },
                )
            ).status_code == 403
            assert received == ["查看第25页"]

    asyncio.run(exercise())
