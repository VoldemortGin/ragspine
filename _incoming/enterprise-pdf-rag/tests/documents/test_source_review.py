"""The business UI exposes persisted source evidence, never synthetic chart QA."""

import asyncio
import json

from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.aia_review import create_aia_app
from enterprise_pdf_rag.documents.models import DocumentSpec


def test_export_and_http_page_navigation_keep_source_identity(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    from enterprise_pdf_rag.adapters.aia_ingestion import export_review

    store, spec = prepared_source
    snapshot = store.load_current()
    export_review(store, snapshot)
    index = (store.root / "review.html").read_text()
    page_path = store.root / "pages" / "page-001.html"
    assert 'href="pages/page-001.html"' in index
    page_html = page_path.read_text()
    assert spec.filename in page_html and "Observed source title" in page_html
    assert snapshot.manifest_id in page_html and "semantics: pending" in page_html
    assert 'href="../review.html"' in page_html
    assert 'href="../source.pdf#page=1"' in page_html
    assert not (store.root / "pages" / "page-000.html").exists()

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_aia_app(store, spec=spec)),
            base_url="http://test",
        ) as client:
            review = await client.get("/v1/aia/review.html")
            assert review.status_code == 200
            page = await client.get("/v1/aia/pages/page-001.html")
            assert page.status_code == 200 and page.text == page_html
            assert (await client.get("/v1/aia/pages/page-000.html")).status_code == 404
            assert (await client.get("/v1/aia/pages/page-002.html")).status_code == 404
            source = await client.get("/v1/aia/source.pdf")
            assert source.content == (store.root / "source.pdf").read_bytes()
            text = await client.get("/v1/aia/text.json")
            assert text.status_code == 200
            assert text.json() == json.loads((store.root / "text.json").read_text())

    asyncio.run(exercise())


def test_source_model_names_file_and_returns_real_observation(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    store, spec = prepared_source

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_aia_app(store, spec=spec)),
            base_url="http://test",
        ) as client:
            models = (await client.get("/v1/models")).json()["data"]
            assert [item["id"] for item in models] == [
                "aia-2026-interim-source-review-v1"
            ]
            answer = await client.post(
                "/v1/chat/completions",
                json={
                    "model": models[0]["id"],
                    "messages": [{"role": "user", "content": "查看第1页"}],
                    "stream": True,
                },
            )
            assert answer.status_code == 200
            packets = [
                json.loads(line.removeprefix("data: "))
                for line in answer.text.splitlines()
                if line.startswith("data: {")
            ]
            assert {packet["model"] for packet in packets} == {models[0]["id"]}
            text = "".join(
                packet["choices"][0]["delta"].get("content", "") for packet in packets
            )
            assert spec.filename in text and "Observed source title" in text
            assert "pending" in text and "原文观测" in text and "第 1 页" in text
            assert "Revenue" not in text and "2024 | 10" not in text
            snapshot = (await client.get("/v1/aia/manifest")).json()
            asset = await client.get(
                "/v1/aia/assets/" + snapshot["manifest"]["source"]["sha256"]
            )
            assert asset.content == b"test-only selected source bytes"
            assert (await client.get("/v1/aia/review")).status_code == 200

    asyncio.run(exercise())


def test_wrong_page_region_sidecar_is_rejected_before_review(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    from dataclasses import asdict, replace

    import pytest

    from enterprise_pdf_rag.adapters.aia_ingestion import (
        read_text_sidecar,
        render_source_review,
    )
    from enterprise_pdf_rag.documents.models import DocumentSnapshot

    store, _spec = prepared_source
    snapshot = store.load_current()
    wrong_sidecar = replace(read_text_sidecar(store, snapshot, 0), page_index=1)
    wrong_ref = store.put(
        json.dumps(asdict(wrong_sidecar)).encode(), media_type="application/json"
    )
    page_one = replace(snapshot.manifest.pages[0], page_index=1, text=wrong_ref)
    wrong_manifest = replace(
        snapshot.manifest,
        pages=(*snapshot.manifest.pages, page_one),
        region=replace(snapshot.manifest.region, text=wrong_ref),
    )
    wrong_id = store.publish(wrong_manifest)
    with pytest.raises(ValueError, match=r"region.*page"):
        render_source_review(store, DocumentSnapshot(wrong_id, wrong_manifest))


def test_region_native_svg_from_another_page_cannot_start_business_profile(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    from dataclasses import replace

    import pytest

    store, spec = prepared_source
    snapshot = store.load_current()
    another_svg = store.put(
        b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M2 2L3 3"/></svg>',
        media_type="image/svg+xml",
    )
    altered = replace(
        snapshot.manifest,
        region=replace(snapshot.manifest.region, native_svg=another_svg),
    )
    store.publish(altered)
    with pytest.raises(ValueError, match="selected source"):
        create_aia_app(store, spec=spec)


def test_missing_source_refuses_answer_before_sse_and_without_demo_fallback(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    store, spec = prepared_source
    app = create_aia_app(store, spec=spec)
    snapshot = store.load_current()
    store.asset_path(snapshot.manifest.source).unlink()

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "aia-2026-interim-source-review-v1",
                    "stream": True,
                    "messages": [{"role": "user", "content": "查看当前文件"}],
                },
            )
            assert response.status_code == 409
            assert "text/event-stream" not in response.headers["content-type"]
            assert "Revenue" not in response.text and "fallback" in response.text

    asyncio.run(exercise())


def test_source_profile_refuses_financial_qa_and_wrong_snapshot(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    store, spec = prepared_source

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=create_aia_app(store, spec=spec)),
            base_url="http://test",
        ) as client:
            base = {
                "model": "aia-2026-interim-source-review-v1",
                "stream": True,
                "messages": [{"role": "user", "content": "查看当前文件"}],
            }
            for patch, status in (
                ({"snapshot_id": "another"}, 409),
                ({"model": "enterprise-pdf-rag-offline-demo-v1"}, 404),
                ({"messages": [{"role": "user", "content": "利润增长多少"}]}, 422),
                ({"messages": [{"role": "user", "content": "查看第72页"}]}, 404),
            ):
                response = await client.post(
                    "/v1/chat/completions", json={**base, **patch}
                )
                assert response.status_code == status
                assert "text/event-stream" not in response.headers["content-type"]

    asyncio.run(exercise())


def test_default_chat_shows_real_processing_status_and_links_not_old_focus_page(
    prepared_source: tuple[LocalDocumentStore, DocumentSpec],
) -> None:
    from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
    from enterprise_pdf_rag.adapters.processing_export import export_processing_review
    from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
    from enterprise_pdf_rag.processing.models import PageInput, PagePartition

    store, spec = prepared_source

    class Partitioner:
        fingerprint = "test-layout"

        def partition(self, page: PageInput) -> PagePartition:
            return PagePartition(
                "layout-v1",
                page.source_manifest_id,
                page.source_sha256,
                page.page_index,
                self.fingerprint,
                (),
                tuple(span.span_id for span in page.text.spans),
                ("test has no visual objects",),
            )

    outputs = ProcessingStore(store.root.parent / "processing")
    processing_id, _ = ProcessingPipeline(store, outputs, Partitioner(), None).run(
        store.load_current().manifest_id, selected_page_indices=(0,)
    )
    export_processing_review(store, outputs, processing_id)

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=create_aia_app(store, spec=spec, processing=outputs)
            ),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "aia-2026-interim-source-review-v1",
                    "messages": [{"role": "user", "content": "查看当前文件"}],
                },
            )
            assert response.status_code == 200
            text = response.json()["choices"][0]["message"]["content"]
            assert "typed IR" in text and "0 份" in text
            assert "/v1/processing/review/review.html" in text
            assert "原文片段" not in text
            assert (await client.get("/v1/processing/status")).json()[
                "processing_id"
            ] == processing_id

    asyncio.run(exercise())
