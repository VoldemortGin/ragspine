"""Configured query embedding reaches the public API without startup inference."""

import asyncio
import json
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http import app as app_module
from enterprise_pdf_rag.adapters.http.aia_review import create_aia_app
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.providers.local_models import LocalEmbeddingAdapter
from ragspine.common.evidence.providers.providers import (
    ProviderRequestError,
    load_local_model_config,
)
from ragspine.common.evidence.settings import get_settings
from ragspine.extraction.evidence.document.models import DocumentSpec
from ragspine.extraction.evidence.figures.models import Confidence
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)


@pytest.mark.parametrize(
    "configuration",
    (
        "valid",
        "missing",
        "partial",
        "remote",
        "provider-failure",
        "wrong-model",
        "wrong-dimensions",
    ),
)
def test_configured_app_search_uses_only_query_embedding_and_preserves_guard(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configuration: str,
) -> None:
    sources, spec = prepared_source
    source_id = sources.load_current().manifest_id
    outputs = ProcessingStore(tmp_path / "processing")

    class Partitioner:
        fingerprint = "test-source-partition"

        def partition(self, page: PageInput) -> PagePartition:
            return PagePartition(
                "layout-v2",
                source_id,
                spec.sha256,
                0,
                self.fingerprint,
                (
                    LayoutObject(
                        "title",
                        ObjectKind.TEXT,
                        spec.focus_bbox,
                        ("page-0-span-0",),
                        "literal",
                        Confidence(None, "test"),
                    ),
                ),
                (),
            )

    _, manifest = ProcessingPipeline(
        sources, outputs, Partitioner(), ProcessingObjectAdapter(sources, outputs)
    ).run(source_id, selected_page_indices=(0,))
    assert manifest.pages[0].objects, repr(manifest)
    environment = {
        "EMBEDDING_BASE_URL": "http://127.0.0.1:9999/v1",
        "EMBEDDING_MODEL": "test-embedding",
        "EMBEDDING_API_KEY": "test-embedding-secret",
    }
    calls: list[dict[str, object]] = []
    query_phase = False

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == "http://127.0.0.1:9999/v1/embeddings"
        assert api_key == "test-embedding-secret" and timeout == 30.0
        calls.append(json.loads(payload))
        if query_phase and configuration == "provider-failure":
            raise ProviderRequestError("provider detail must stay internal")
        if query_phase and configuration == "wrong-dimensions":
            return b'{"data":[{"index":0,"embedding":[1.0]}]}'
        return b'{"data":[{"index":0,"embedding":[1.0,0.0]}]}'

    embedder = LocalEmbeddingAdapter(
        load_local_model_config("embedding", environment), sender=sender
    )
    publication = ProcessingRetrieval(sources, outputs, embedder).build(
        manifest.scope,
        tuple((page.page_index, item) for page in manifest.pages for item in page.objects),
    )
    processing_id = outputs.publish(replace(manifest, retrieval=publication), sources=sources)
    assert calls, repr(manifest)
    calls.clear()
    query_phase = True
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if configuration == "missing":
        for name in environment:
            monkeypatch.delenv(name)
    elif configuration == "partial":
        monkeypatch.delenv("EMBEDDING_API_KEY")
    elif configuration == "remote":
        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://unapproved.example/v1")
    elif configuration == "wrong-model":
        monkeypatch.setenv("EMBEDDING_MODEL", "other-model")
    monkeypatch.setenv("APP_EXECUTION_MODE", "aia-source-review")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-llm-secret")
    monkeypatch.setattr(app_module, "AIA_OUTPUT", sources.root)
    monkeypatch.setattr(app_module, "PROCESSING_OUTPUT", outputs.root)
    monkeypatch.setattr(app_module, "create_aia_app", partial(create_aia_app, spec=spec))
    monkeypatch.setattr("ragspine.common.evidence.providers.local_models._send_local_once", sender)
    get_settings.cache_clear()
    try:
        application = app_module.create_configured_app()
        assert calls == []

        async def exercise() -> None:
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                assert (await client.get("/v1/models")).status_code == 200
                assert calls == []
                response = await client.post(
                    "/v1/processing/search",
                    json={
                        "processing_id": processing_id,
                        "query": "Observed source title",
                        "limit": 1,
                    },
                )
                if configuration != "valid":
                    expected = 409 if configuration in {"wrong-model", "wrong-dimensions"} else 503
                    assert response.status_code == expected, response.text
                    assert "test-embedding-secret" not in response.text
                    assert "provider detail" not in response.text
                    return
                assert response.status_code == 200, response.text
                hit = response.json()["hits"][0]
                context = await client.post(
                    "/v1/processing/context",
                    json={"processing_id": processing_id, "hit": hit},
                )
                assert context.status_code == 200
                assert (
                    context.json()["context"]["qualification"]["scope"]
                    == "literal-source-transcription-v1"
                )
                refused = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "aia-2026-interim-source-review-v1",
                        "messages": [{"role": "user", "content": "利润增长多少"}],
                    },
                )
                assert refused.status_code == 422
                assert "test-embedding-secret" not in response.text + context.text + refused.text
                assert "unrelated-llm-secret" not in response.text + context.text + refused.text

        asyncio.run(exercise())
        expected_calls = (
            []
            if configuration in {"missing", "partial", "remote", "wrong-model"}
            else [
                {
                    "model": "test-embedding",
                    "input": "Observed source title",
                    "encoding_format": "float",
                }
            ]
        )
        assert calls == expected_calls
        assert "test-embedding-secret" not in caplog.text
        assert "unrelated-llm-secret" not in caplog.text
    finally:
        get_settings.cache_clear()
