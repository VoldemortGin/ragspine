"""Published documents are listed, searched and resolved by id over HTTP without fabrication."""

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.draft_publication import DraftPublication
from enterprise_pdf_rag.adapters.http import app as app_module
from enterprise_pdf_rag.adapters.http.documents import create_documents_app
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.providers import LocalModelConfig, ProviderRequestError
from enterprise_pdf_rag.core.settings import get_settings
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    publish_generic_document,
)
from tests.enterprise_pdf_rag.adapters.test_document_catalog import CountingEmbedder
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding

_MISSING_ID = "f" * 64
_SECRET = "test-embedding-secret"
_OFFLINE_FINGERPRINT = "offline-demo/token-hash-64-v1"
_EMBEDDING_ENV = {
    "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
    "EMBEDDING_MODEL": "test-embedding",
    "EMBEDDING_API_KEY": _SECRET,
}
_ROUTES = {
    "/v1/documents",
    "/v1/documents/{document_id}",
    "/v1/documents/{document_id}/manifest",
    "/v1/documents/{document_id}/search",
    "/v1/documents/{document_id}/context",
}


class FailingEmbedder:
    """Carries the published fingerprint but every query embedding fails at the provider."""

    fingerprint = _OFFLINE_FINGERPRINT

    def embed_description(self, text: str) -> tuple[float, ...]:
        raise AssertionError("Descriptions are never embedded at query time")

    def embed_query(self, text: str) -> tuple[float, ...]:
        raise ProviderRequestError(f"provider detail {_SECRET}", status=500, category="http")


Published = tuple[Path, DraftPublication, DraftPublication, DraftPublication]


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Published:
    """Two ready documents plus one whose discovery pointer broke after publication."""
    root = tmp_path / "ingestion"
    embedder = OfflineDescriptionEmbedder()
    documents = [
        publish_generic_document(
            tmp_path,
            monkeypatch,
            filename=filename,
            label=label,
            page_count=pages,
            embedder=embedder,
            output_dir=root,
        )
        for filename, label, pages in (
            ("meridian.pdf", "Meridian revenue", 3),
            ("orion.pdf", "Orion expense", 2),
            ("vega.pdf", "Vega margin", 1),
        )
    ]
    meridian, orion, vega = documents
    (Path(vega.processing_store) / "current-processing").write_text(_MISSING_ID + "\n")
    return root, meridian, orion, vega


def _run(app: FastAPI, scenario: Callable[[AsyncClient], Awaitable[None]]) -> None:
    async def exercise() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await scenario(client)

    asyncio.run(exercise())


def _route_paths(app: FastAPI) -> set[str]:
    """Flatten included routers (FastAPI keeps them as one lazy route entry)."""
    paths: set[str] = set()
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        nested = getattr(route, "original_router", None)
        if nested is not None:
            pending.extend(nested.routes)
        elif isinstance(path := getattr(route, "path", None), str):
            paths.add(path)
    return paths


def _resolve_offline(
    root: Path, publication: DraftPublication, hit: dict[str, object]
) -> RetrievalContext:
    """Cross-check an HTTP hit through the Python mount with no embedder at all."""
    entry = scan_catalog(root).entry(publication.source_sha256)
    assert entry is not None
    snapshot_id, member_id, score = hit["snapshot_id"], hit["member_id"], hit["score"]
    assert isinstance(snapshot_id, str) and isinstance(member_id, str)
    assert isinstance(score, float)
    return mount_document(entry, embedder=None).resolve(
        PinnedRetrievalHit(snapshot_id, member_id, score)
    )


async def _first_hit(client: AsyncClient, document_id: str, query: str) -> dict[str, object]:
    response = await client.post(
        f"/v1/documents/{document_id}/search", json={"query": query, "limit": 1}
    )
    assert response.status_code == 200, response.text
    hit = response.json()["hits"][0]
    assert isinstance(hit, dict)
    return hit


def test_list_documents_reports_mount_state_without_model_calls(published: Published) -> None:
    root, meridian, orion, vega = published
    embedder = CountingEmbedder(OfflineDescriptionEmbedder())
    app = create_documents_app(scan_catalog(root), embedder=embedder)
    assert _route_paths(app) >= _ROUTES

    async def scenario(client: AsyncClient) -> None:
        response = await client.get("/v1/documents")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schema_version"] == "document-catalog-v1"
        assert payload["ingestion_root"] == str(root.resolve())
        assert payload["legacy_roots"] == []
        assert payload["embedding_configured"] is True
        assert payload["embedding_fingerprint"] == _OFFLINE_FINGERPRINT
        assert payload["unpublished"] == []
        items = {item["document_id"]: item for item in payload["documents"]}
        assert list(items) == sorted(
            (meridian.source_sha256, orion.source_sha256, vega.source_sha256)
        )
        for publication, label in ((meridian, "meridian.pdf"), (orion, "orion.pdf")):
            item = items[publication.source_sha256]
            assert item["retrieval_status"] == "ready"
            assert item["mounted"] is True and item["mount_error"] is None
            assert item["origin"] == "ingestion"
            assert item["document_label"] == label
            assert item["current_processing_id"] == publication.current_processing_id
            assert item["retrieval_snapshot_id"] == publication.retrieval_snapshot_id
            assert item["embedding_fingerprint"] == _OFFLINE_FINGERPRINT
        broken = items[vega.source_sha256]
        assert broken["retrieval_status"] == "corrupt"
        assert broken["mounted"] is False
        assert broken["reason"] and broken["mount_error"] == broken["reason"]

    _run(app, scenario)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)


def test_detail_unknown_404_and_corrupt_200_with_reason(published: Published) -> None:
    root, meridian, _, vega = published
    app = create_documents_app(scan_catalog(root), embedder=OfflineDescriptionEmbedder())

    async def scenario(client: AsyncClient) -> None:
        for unknown in (_MISSING_ID, "not-a-document"):
            assert (await client.get(f"/v1/documents/{unknown}")).status_code == 404

        corrupt = await client.get(f"/v1/documents/{vega.source_sha256}")
        assert corrupt.status_code == 200, corrupt.text
        payload = corrupt.json()
        assert payload["schema_version"] == "document-catalog-v1"
        assert payload["document"]["retrieval_status"] == "corrupt"
        assert payload["document"]["mounted"] is False
        assert payload["document"]["reason"]
        assert payload["status"] is None

        ready = await client.get(f"/v1/documents/{meridian.source_sha256}")
        assert ready.status_code == 200, ready.text
        detail = ready.json()
        assert detail["document"]["mounted"] is True
        assert detail["status"]["processing_id"] == meridian.current_processing_id
        assert detail["status"]["retrieval_snapshot_id"] == meridian.retrieval_snapshot_id
        assert detail["status"]["source_page_count"] == 3
        assert detail["status"]["selected_physical_pages"] == [1, 2, 3]

    _run(app, scenario)


def test_manifest_is_the_pinned_processing_release(published: Published) -> None:
    root, meridian, _, vega = published
    app = create_documents_app(scan_catalog(root), embedder=OfflineDescriptionEmbedder())

    async def scenario(client: AsyncClient) -> None:
        response = await client.get(f"/v1/documents/{meridian.source_sha256}/manifest")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["snapshot_id"] == meridian.current_processing_id
        assert payload["manifest"]["scope"]["source_sha256"] == meridian.source_sha256
        assert payload["manifest"]["retrieval"]["snapshot_id"] == meridian.retrieval_snapshot_id
        assert (await client.get(f"/v1/documents/{_MISSING_ID}/manifest")).status_code == 404
        corrupt = await client.get(f"/v1/documents/{vega.source_sha256}/manifest")
        assert corrupt.status_code == 409, corrupt.text

    _run(app, scenario)


def test_search_corrupt_409_carries_reason(published: Published) -> None:
    root, _, _, vega = published
    catalog = scan_catalog(root)
    broken = catalog.entry(vega.source_sha256)
    assert broken is not None and broken.reason is not None
    app = create_documents_app(catalog, embedder=OfflineDescriptionEmbedder())

    async def scenario(client: AsyncClient) -> None:
        response = await client.post(
            f"/v1/documents/{vega.source_sha256}/search", json={"query": "Vega margin"}
        )
        assert response.status_code == 409, response.text
        assert broken.reason is not None and broken.reason in response.json()["detail"]
        missing = await client.post(
            f"/v1/documents/{_MISSING_ID}/search", json={"query": "Vega margin"}
        )
        assert missing.status_code == 404

    _run(app, scenario)


def test_search_and_context_roundtrip_makes_exactly_one_query_embedding(
    published: Published,
) -> None:
    root, meridian, _, _ = published
    embedder = CountingEmbedder(OfflineDescriptionEmbedder())
    app = create_documents_app(scan_catalog(root), embedder=embedder)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)

    async def scenario(client: AsyncClient) -> None:
        document_id = meridian.source_sha256
        await client.get("/v1/documents")
        await client.get(f"/v1/documents/{document_id}")
        await client.get(f"/v1/documents/{document_id}/manifest")
        assert (embedder.description_calls, embedder.query_calls) == (0, 0)

        search = await client.post(
            f"/v1/documents/{document_id}/search", json={"query": "Meridian revenue", "limit": 5}
        )
        assert search.status_code == 200, search.text
        results = search.json()
        assert results["document_id"] == document_id
        assert results["processing_id"] == meridian.current_processing_id
        assert results["snapshot_id"] == meridian.retrieval_snapshot_id
        assert results["hits"]
        assert all(hit["snapshot_id"] == meridian.retrieval_snapshot_id for hit in results["hits"])
        assert (embedder.description_calls, embedder.query_calls) == (0, 1)

        hit = results["hits"][0]
        context = await client.post(f"/v1/documents/{document_id}/context", json={"hit": hit})
        assert context.status_code == 200, context.text
        payload = context.json()
        assert payload["document_id"] == document_id
        assert payload["processing_id"] == meridian.current_processing_id
        expected = _resolve_offline(root, meridian, hit)
        assert payload["context"]["snapshot_id"] == hit["snapshot_id"]
        assert payload["context"]["member"]["object_id"] == expected.member.object_id
        assert payload["context"]["member"]["page_index"] == expected.member.page_index
        assert payload["context"]["description"]["text"] == expected.description.text
        assert "Meridian" in payload["context"]["description"]["text"]
        assert payload["context"]["qualification"]["scope"] == "literal-source-transcription-v1"
        assert (embedder.description_calls, embedder.query_calls) == (0, 1)

        for invalid in ({"query": ""}, {"query": "x", "limit": 0}, {"query": "x", "extra": 1}):
            response = await client.post(f"/v1/documents/{document_id}/search", json=invalid)
            assert response.status_code == 422, response.text
        assert (embedder.description_calls, embedder.query_calls) == (0, 1)

    _run(app, scenario)


def test_context_cross_document_409(published: Published) -> None:
    root, meridian, orion, _ = published
    app = create_documents_app(scan_catalog(root), embedder=OfflineDescriptionEmbedder())

    async def scenario(client: AsyncClient) -> None:
        foreign = await _first_hit(client, orion.source_sha256, "Orion expense")
        assert foreign["snapshot_id"] == orion.retrieval_snapshot_id
        response = await client.post(
            f"/v1/documents/{meridian.source_sha256}/context", json={"hit": foreign}
        )
        assert response.status_code == 409, response.text
        own = await client.post(
            f"/v1/documents/{orion.source_sha256}/context", json={"hit": foreign}
        )
        assert own.status_code == 200, own.text

    _run(app, scenario)


def test_search_without_embedder_503_but_list_and_context_work(published: Published) -> None:
    root, meridian, orion, _ = published
    catalog = scan_catalog(root)
    searching = create_documents_app(catalog, embedder=OfflineDescriptionEmbedder())
    evidence_only = create_documents_app(catalog, embedder=None)
    hits: list[dict[str, object]] = []

    async def collect(client: AsyncClient) -> None:
        hits.append(await _first_hit(client, meridian.source_sha256, "Meridian revenue"))

    _run(searching, collect)

    async def scenario(client: AsyncClient) -> None:
        listing = await client.get("/v1/documents")
        assert listing.status_code == 200, listing.text
        payload = listing.json()
        assert payload["embedding_configured"] is False
        assert payload["embedding_fingerprint"] is None
        mounted = {item["document_id"]: item["mounted"] for item in payload["documents"]}
        assert mounted[meridian.source_sha256] is True and mounted[orion.source_sha256] is True

        search = await client.post(
            f"/v1/documents/{meridian.source_sha256}/search", json={"query": "Meridian revenue"}
        )
        assert search.status_code == 503, search.text
        assert "not configured" in search.json()["detail"]

        context = await client.post(
            f"/v1/documents/{meridian.source_sha256}/context", json={"hit": hits[0]}
        )
        assert context.status_code == 200, context.text
        expected = _resolve_offline(root, meridian, hits[0])
        assert context.json()["context"]["member"]["object_id"] == expected.member.object_id
        manifest = await client.get(f"/v1/documents/{meridian.source_sha256}/manifest")
        assert manifest.status_code == 200

    _run(evidence_only, scenario)


def test_search_wrong_fingerprint_is_unmounted_409(published: Published) -> None:
    root, meridian, orion, vega = published
    embedder = CountingEmbedder(RecordingEmbedding())
    app = create_documents_app(scan_catalog(root), embedder=embedder)

    async def scenario(client: AsyncClient) -> None:
        listing = await client.get("/v1/documents")
        assert listing.status_code == 200, listing.text
        payload = listing.json()
        assert payload["embedding_configured"] is True
        assert payload["embedding_fingerprint"] == RecordingEmbedding.fingerprint
        items = {item["document_id"]: item for item in payload["documents"]}
        for publication in (meridian, orion):
            item = items[publication.source_sha256]
            assert item["retrieval_status"] == "ready" and item["mounted"] is False
            assert "provider" in item["mount_error"]
        assert items[vega.source_sha256]["mounted"] is False

        document_id = meridian.source_sha256
        search = await client.post(
            f"/v1/documents/{document_id}/search", json={"query": "Meridian revenue"}
        )
        assert search.status_code == 409, search.text
        assert "provider" in search.json()["detail"]
        assert (await client.get(f"/v1/documents/{document_id}/manifest")).status_code == 409
        detail = await client.get(f"/v1/documents/{document_id}")
        assert detail.status_code == 200 and detail.json()["status"] is None
        hit = {
            "snapshot_id": meridian.retrieval_snapshot_id,
            "member_id": _MISSING_ID,
            "score": 0.0,
        }
        context = await client.post(f"/v1/documents/{document_id}/context", json={"hit": hit})
        assert context.status_code == 409

    _run(app, scenario)
    assert (embedder.description_calls, embedder.query_calls) == (0, 0)


def test_search_provider_failure_503_does_not_leak_secrets(published: Published) -> None:
    root, meridian, _, _ = published
    app = create_documents_app(scan_catalog(root), embedder=FailingEmbedder())

    async def scenario(client: AsyncClient) -> None:
        listing = await client.get("/v1/documents")
        assert listing.status_code == 200
        assert listing.json()["documents"][0]["mounted"] is True
        response = await client.post(
            f"/v1/documents/{meridian.source_sha256}/search", json={"query": "Meridian revenue"}
        )
        assert response.status_code == 503, response.text
        assert _SECRET not in response.text
        assert "provider detail" not in response.text

    _run(app, scenario)


def test_tampered_pinned_manifest_is_refused_with_409(published: Published) -> None:
    root, meridian, _, _ = published
    app = create_documents_app(scan_catalog(root), embedder=OfflineDescriptionEmbedder())
    document_id = meridian.source_sha256
    hits: list[dict[str, object]] = []

    async def collect(client: AsyncClient) -> None:
        hits.append(await _first_hit(client, document_id, "Meridian revenue"))

    _run(app, collect)
    pinned = Path(meridian.processing_store) / "objects" / "sha256" / meridian.current_processing_id
    assert pinned.is_file()
    pinned.write_bytes(b"tampered after mount")

    async def scenario(client: AsyncClient) -> None:
        listing = await client.get("/v1/documents")
        assert listing.status_code == 200
        assert (await client.get(f"/v1/documents/{document_id}")).status_code == 409
        assert (await client.get(f"/v1/documents/{document_id}/manifest")).status_code == 409
        search = await client.post(
            f"/v1/documents/{document_id}/search", json={"query": "Meridian revenue"}
        )
        assert search.status_code == 409, search.text
        context = await client.post(f"/v1/documents/{document_id}/context", json={"hit": hits[0]})
        assert context.status_code == 409, context.text

    _run(app, scenario)


def test_unconfigured_message_names_document_catalog_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_EXECUTION_MODE", "unconfigured")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="document-catalog"):
            app_module.create_configured_app()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("configuration", ("valid", "missing"))
def test_configured_app_document_catalog_mode(
    published: Published, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    root, meridian, orion, _ = published
    constructed: list[LocalModelConfig] = []

    def fake_adapter(config: LocalModelConfig) -> OfflineDescriptionEmbedder:
        constructed.append(config)
        return OfflineDescriptionEmbedder()

    monkeypatch.setenv("APP_EXECUTION_MODE", "document-catalog")
    monkeypatch.setenv("APP_INGESTION_DIR", str(root))
    monkeypatch.delenv("APP_LEGACY_DOCUMENT_ROOTS", raising=False)
    for name, value in _EMBEDDING_ENV.items():
        if configuration == "valid":
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app_module, "LocalEmbeddingAdapter", fake_adapter)
    get_settings.cache_clear()
    try:
        application = app_module.create_configured_app()
        if configuration == "valid":
            assert len(constructed) == 1
            assert constructed[0].model == "test-embedding"
            assert constructed[0].api_key.get_secret_value() == _SECRET
        else:
            assert constructed == []
        assert _route_paths(application) >= _ROUTES
        entry = scan_catalog(root).entry(meridian.source_sha256)
        assert entry is not None
        member_id = mount_document(entry, embedder=None).member_texts()[0].member_id
        known_hit = {
            "snapshot_id": meridian.retrieval_snapshot_id,
            "member_id": member_id,
            "score": 0.0,
        }

        async def scenario(client: AsyncClient) -> None:
            listing = await client.get("/v1/documents")
            assert listing.status_code == 200, listing.text
            payload = listing.json()
            assert payload["ingestion_root"] == str(root.resolve())
            assert payload["embedding_configured"] is (configuration == "valid")
            mounted = {item["document_id"]: item["mounted"] for item in payload["documents"]}
            assert mounted[meridian.source_sha256] is True
            assert mounted[orion.source_sha256] is True
            search = await client.post(
                f"/v1/documents/{meridian.source_sha256}/search",
                json={"query": "Meridian revenue", "limit": 1},
            )
            if configuration == "valid":
                assert search.status_code == 200, search.text
                assert search.json()["hits"][0]["snapshot_id"] == meridian.retrieval_snapshot_id
            else:
                assert search.status_code == 503, search.text
            assert _SECRET not in listing.text + search.text
            context = await client.post(
                f"/v1/documents/{meridian.source_sha256}/context", json={"hit": known_hit}
            )
            assert context.status_code == 200, context.text
            assert _SECRET not in context.text

        _run(application, scenario)
    finally:
        get_settings.cache_clear()


def test_configured_app_legacy_root_env(
    published: Published, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, meridian, _, _ = published
    legacy = tmp_path / "legacy-source"
    shutil.copytree(meridian.source_store, legacy)
    shutil.copytree(meridian.processing_store, legacy / "release")
    absent = tmp_path / "absent"

    monkeypatch.setenv("APP_EXECUTION_MODE", "document-catalog")
    monkeypatch.setenv("APP_INGESTION_DIR", str(absent))
    monkeypatch.setenv("APP_LEGACY_DOCUMENT_ROOTS", json.dumps([str(legacy / "release")]))
    for name in _EMBEDDING_ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        application = app_module.create_configured_app()

        async def scenario(client: AsyncClient) -> None:
            listing = await client.get("/v1/documents")
            assert listing.status_code == 200, listing.text
            payload = listing.json()
            assert payload["ingestion_root"] == str(absent.resolve())
            assert payload["legacy_roots"] == [str((legacy / "release").resolve())]
            assert payload["embedding_configured"] is False
            assert payload["unpublished"] == []
            assert len(payload["documents"]) == 1
            item = payload["documents"][0]
            assert item["origin"] == "legacy"
            assert item["document_id"] == meridian.source_sha256
            assert item["retrieval_status"] == "ready" and item["mounted"] is True
            assert item["processing_store"] == str((legacy / "release").resolve())
            assert item["source_store"] == str(legacy.resolve())
            search = await client.post(
                f"/v1/documents/{meridian.source_sha256}/search", json={"query": "Meridian"}
            )
            assert search.status_code == 503, search.text
            manifest = await client.get(f"/v1/documents/{meridian.source_sha256}/manifest")
            assert manifest.status_code == 200, manifest.text
            assert manifest.json()["snapshot_id"] == meridian.current_processing_id

        _run(application, scenario)
    finally:
        get_settings.cache_clear()
