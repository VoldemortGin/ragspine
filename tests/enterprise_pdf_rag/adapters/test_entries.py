"""Public entry points execute the same pinned, explicitly offline use case."""

import asyncio
import json
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient
from pytest import CaptureFixture

from enterprise_pdf_rag.adapters.http.app import create_app
from enterprise_pdf_rag.adapters.runtime import create_runtime
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.figures.models import ExecutionMode


def test_cli_demo_writes_reviewable_artifacts(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert main(["demo", "--mode", "offline-demo", "--output", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["execution_mode"] == "offline-demo"
    assert result["context"]["chart_ir"]["points"][1]["value"]["value"] == "15"
    assert (tmp_path / "source.pdf").read_bytes().startswith(b"%PDF-")
    assert "<svg" in (tmp_path / "review.html").read_text()


def test_api_search_hit_resolves_in_its_snapshot() -> None:
    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(mode=ExecutionMode.OFFLINE_DEMO)),
            base_url="http://test",
        ) as client:
            built = await client.post("/v1/demo/ingest", json={"snapshot_id": "api-v1"})
            assert built.status_code == 201
            found = await client.post(
                "/v1/search", json={"snapshot_id": "api-v1", "query": "Revenue 2025"}
            )
            assert found.status_code == 200
            hit = found.json()["hits"][0]
            resolved = await client.post("/v1/context", json={"snapshot_id": "api-v1", "hit": hit})
            assert resolved.status_code == 200
            assert (
                resolved.json()["context"]["chart_ir"]["binding"]["svg_digest"] == hit["svg_digest"]
            )
            wrong = await client.post("/v1/context", json={"snapshot_id": "other", "hit": hit})
            assert wrong.status_code == 409
            empty = await client.post("/v1/search", json={"snapshot_id": "api-v1", "query": "   "})
            assert empty.status_code == 422
            coerced = await client.post(
                "/v1/search",
                json={"snapshot_id": "api-v1", "query": "Revenue", "limit": "5"},
            )
            assert coerced.status_code == 422

    asyncio.run(exercise())


def test_production_does_not_fall_back_to_offline_adapters() -> None:
    with pytest.raises(ValueError, match="no mock fallback"):
        create_runtime(mode=ExecutionMode.PRODUCTION)


def test_llm_smoke_requires_an_explicit_model_without_secret_output(
    monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(SystemExit) as caught:
        main(["llm-smoke"])
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "OPENAI_MODEL" in error
    assert "test-secret" not in error
