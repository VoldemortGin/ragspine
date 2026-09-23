"""OpenAI-compatible transport renders only qualified demo evidence."""

import asyncio
import json

import pytest
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.http.app import create_app
from enterprise_pdf_rag.adapters.memory import MemoryFigureRepository
from ragspine.extraction.evidence.figures.models import ChartIR, ExecutionMode


def test_model_and_nonstream_completion_return_real_demo_evidence() -> None:
    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(mode=ExecutionMode.OFFLINE_DEMO)),
            base_url="http://test",
        ) as client:
            models = await client.get("/v1/models")
            assert models.status_code == 200
            model = models.json()["data"][0]["id"]
            assert "offline-demo" in model
            answer = await client.post(
                "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Show demo chart evidence"}],
                    "stream": False,
                },
            )
            assert answer.status_code == 200
            content = answer.json()["choices"][0]["message"]["content"]
            assert "offline-demo" in content
            assert "2024 | 10 | USDm" in content
            assert "2025 | 15 | USDm" in content
            assert "PDF SHA-256" not in content
            assert "<details>" not in content
            assert "标签坐标" in content
            assert "合成演示 PDF 第 1 页" in content

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"model": "gpt-5.6-luna"}, 404),
        ({"snapshot_id": "other"}, 409),
        (
            {"messages": [{"role": "user", "content": "What is AIA profit in 2026?"}]},
            422,
        ),
        ({"tools": [{"type": "function"}]}, 422),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/a.png"},
                            }
                        ],
                    }
                ]
            },
            422,
        ),
    ],
)
def test_unsupported_requests_fail_before_any_answer(
    change: dict[str, object], status: int
) -> None:
    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(mode=ExecutionMode.OFFLINE_DEMO)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "enterprise-pdf-rag-offline-demo-v1",
                    "messages": [{"role": "user", "content": "Revenue"}],
                    "stream": True,
                    **change,
                },
            )
            assert response.status_code == status
            assert "text/event-stream" not in response.headers["content-type"]
            assert "2025 | 15" not in response.text

    asyncio.run(exercise())


def test_missing_chart_is_rejected_before_stream_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(mode=ExecutionMode.OFFLINE_DEMO)

    def missing_chart(_self: MemoryFigureRepository, _artifact_id: str) -> ChartIR | None:
        return None

    monkeypatch.setattr(MemoryFigureRepository, "get_chart", missing_chart)

    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "enterprise-pdf-rag-offline-demo-v1",
                    "messages": [{"role": "user", "content": "Revenue"}],
                    "stream": True,
                },
            )
            assert response.status_code == 404
            assert "no summary fallback" in response.text
            assert "text/event-stream" not in response.headers["content-type"]

    asyncio.run(exercise())


def test_stream_has_role_content_stop_and_done_for_the_same_verified_answer() -> None:
    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(mode=ExecutionMode.OFFLINE_DEMO)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "enterprise-pdf-rag-offline-demo-v1",
                    "messages": [{"role": "user", "content": "展示演示图表证据"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            packets = [
                line.removeprefix("data: ")
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert packets[-1] == "[DONE]"
            events = [json.loads(packet) for packet in packets[:-1]]
            assert events[0]["choices"][0]["delta"]["role"] == "assistant"
            assert events[-1]["choices"][0]["finish_reason"] == "stop"
            text = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
            assert "2024 | 10 | USDm" in text and "2025 | 15 | USDm" in text
            assert "PDF SHA-256" not in text
            assert "标签坐标" in text
            assert len({event["id"] for event in events}) == 1

    asyncio.run(exercise())
