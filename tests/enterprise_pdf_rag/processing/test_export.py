"""Layout review exposes the selected pages and deferred semantics honestly."""

import asyncio
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.processing_review import create_processing_router
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.document.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from ragspine.extraction.evidence.figures.models import Confidence
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)


def test_layout_only_review_does_not_claim_semantics_completed(tmp_path: Path) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    pdf = sources.put(b"unit source", media_type="application/pdf")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><path d="M0 0L10 10"/></svg>',
        media_type="image/svg+xml",
    )
    sidecar = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        (TextSpan("label", "Title", (1.0, 1.0, 20.0, 10.0)),),
    )
    text = sources.put(json.dumps(asdict(sidecar)).encode(), media_type="application/json")
    source_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "test.pdf",
            pdf,
            "test",
            (PageRecord(0, 100.0, 100.0, 0, svg, text, 1, ()),),
            RegionRecord(0, (0.0, 0.0, 100.0, 100.0), svg, svg, text, ()),
        )
    )

    class Partition:
        fingerprint = "layout-test-v1"

        def partition(self, page: PageInput) -> PagePartition:
            return PagePartition(
                "layout-v2",
                source_id,
                pdf.sha256,
                0,
                self.fingerprint,
                (
                    LayoutObject(
                        "object",
                        ObjectKind.TEXT,
                        (0.0, 0.0, 100.0, 100.0),
                        ("label",),
                        "inferred",
                        Confidence(None, "test"),
                    ),
                ),
                (),
            )

    outputs = ProcessingStore(tmp_path / "processing")
    snapshot_id, manifest = ProcessingPipeline(sources, outputs, Partition(), None).run(
        source_id, selected_page_indices=(0,)
    )
    assert manifest.pages[0].objects[0].stages[0].state == "deferred"
    controls = b'{"scope":"bounded service smoke only","ranking_benchmark_qualified":false}'
    controls_id = sha256(controls).hexdigest()
    controls_relative = f"retrieval-controls/{controls_id}/controls.json"
    controls_path = outputs.root / "runs" / snapshot_id / controls_relative
    controls_path.parent.mkdir(parents=True)
    controls_path.write_bytes(controls)
    review = export_processing_review(sources, outputs, snapshot_id)
    assert review == outputs.root / "review.html"
    assert "语义尚未完成" in review.read_text()
    run = outputs.root / "runs" / snapshot_id
    page = run / "page-001" / "review.html"
    assert page.is_file() and "deferred" in page.read_text()
    assert (run / "page-001" / "canonical.json").is_file()
    assert (run / "page-001" / "layout.json").is_file()
    assert not tuple(run.rglob("ir.json"))
    assert (run / "coverage.json").is_file()
    assert "仅标签资格" in (run / "review.html").read_text()
    assert controls_relative in (run / "review.html").read_text()
    assert "整体排序 benchmark" in (run / "review.html").read_text()
    assert json.loads((run / "coverage.json").read_text())[0]["ir_artifacts"] == 0
    assert export_processing_review(sources, outputs, snapshot_id) == review

    async def exercise() -> None:
        app = FastAPI()
        app.include_router(create_processing_router(sources, outputs, snapshot_id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/processing/status")
            assert response.status_code == 200
            assert response.json()["source_page_count"] == 1
            assert response.json()["ir_artifacts"] == 0
            assert response.json()["deferred_stages"] == 1
            assert (
                await client.get("/v1/processing/review/page-001/review.html")
            ).status_code == 200
            assert (
                await client.get("/v1/processing/review/page-021/review.html")
            ).status_code == 404
            refused = await client.post(
                "/v1/processing/search",
                json={"processing_id": snapshot_id, "query": "Title", "limit": 1},
            )
            assert refused.status_code == 409
            context = await client.post(
                "/v1/processing/context",
                json={
                    "processing_id": snapshot_id,
                    "hit": {
                        "snapshot_id": "a" * 64,
                        "member_id": "b" * 64,
                        "score": 0.5,
                    },
                },
            )
            assert context.status_code == 409
            control_url = "/v1/processing/review/" + controls_relative
            control_response = await client.get(control_url)
            assert control_response.status_code == 200
            assert control_response.content == controls
            controls_path.write_bytes(b"changed control result")
            assert (await client.get(control_url)).status_code == 409

    asyncio.run(exercise())
