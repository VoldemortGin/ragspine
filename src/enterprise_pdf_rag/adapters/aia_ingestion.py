"""The explicitly selected AIA file, persisted without any model invocation."""

import json
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.source_review_html import render_index, render_page
from enterprise_pdf_rag.core.settings import DATA_DIR
from enterprise_pdf_rag.documents.aia import AIA_SPEC
from enterprise_pdf_rag.documents.models import DocumentSnapshot, TextSidecar
from enterprise_pdf_rag.documents.ports import SourceExtractor
from enterprise_pdf_rag.documents.service import ingest_document

AIA_OUTPUT = DATA_DIR / "output" / "aia-2026-interim"
AIA_INPUT = DATA_DIR / "samples" / AIA_SPEC.filename


def read_text_sidecar(
    store: LocalDocumentStore, snapshot: DocumentSnapshot, page_index: int
) -> TextSidecar:
    page = snapshot.manifest.pages[page_index]
    sidecar = TypeAdapter(TextSidecar).validate_json(store.get(page.text), strict=True)
    if sidecar.page_index != page_index or sidecar.source_sha256 != snapshot.manifest.source.sha256:
        raise ValueError("Text sidecar is not bound to this source page")
    return sidecar


def read_region_sidecar(store: LocalDocumentStore, snapshot: DocumentSnapshot) -> TextSidecar:
    manifest = snapshot.manifest
    region = manifest.region
    if (
        not 0 <= region.page_index < len(manifest.pages)
        or region.native_svg != manifest.pages[region.page_index].svg
    ):
        raise ValueError("Source region native SVG is not bound to its page")
    sidecar = TypeAdapter(TextSidecar).validate_json(store.get(region.text), strict=True)
    if sidecar.page_index != region.page_index or sidecar.source_sha256 != manifest.source.sha256:
        raise ValueError("Source region text is not bound to this source page")
    observed = {
        span.span_id: span for span in read_text_sidecar(store, snapshot, region.page_index).spans
    }
    if any(observed.get(span.span_id) != span for span in sidecar.spans):
        raise ValueError("Source region span is not an observation of this page")
    return sidecar


def render_source_review(store: LocalDocumentStore, snapshot: DocumentSnapshot) -> str:
    return render_index(
        snapshot=snapshot,
        region_svg=store.get(snapshot.manifest.region.cropped_svg).decode(),
        region_text=read_region_sidecar(store, snapshot),
    )


def render_source_page(
    store: LocalDocumentStore, snapshot: DocumentSnapshot, page_index: int
) -> str:
    return render_page(
        snapshot=snapshot,
        page_index=page_index,
        svg=store.get(snapshot.manifest.pages[page_index].svg).decode(),
        text=read_text_sidecar(store, snapshot, page_index),
    )


def source_text_json(store: LocalDocumentStore, snapshot: DocumentSnapshot) -> str:
    sidecars = [
        asdict(read_text_sidecar(store, snapshot, i)) for i in range(len(snapshot.manifest.pages))
    ]
    return (
        json.dumps(
            {
                "schema_version": "source-text-export-v1",
                "manifest_id": snapshot.manifest_id,
                "source_sha256": snapshot.manifest.source.sha256,
                "pages": sidecars,
                "interpretation": "raw text observations; not semantic chart extraction",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def export_review(store: LocalDocumentStore, snapshot: DocumentSnapshot) -> None:
    output = store.root
    (output / "source.pdf").write_bytes(store.get(snapshot.manifest.source))
    (output / "text.json").write_text(source_text_json(store, snapshot))
    for name in ("chart-ir", "description"):
        (output / f"{name}.status.json").write_text(
            json.dumps(
                {
                    "schema_version": "semantic-status-v1",
                    "artifact": name,
                    "status": "pending",
                    "artifact_id": None,
                    "manifest_id": snapshot.manifest_id,
                    "source_sha256": snapshot.manifest.source.sha256,
                    "reason": "No source-qualified semantic extraction has been performed; no summary fallback or embedding",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    pages = output / "pages"
    pages.mkdir(exist_ok=True)
    for page_index in range(len(snapshot.manifest.pages)):
        (pages / f"page-{page_index + 1:03d}.html").write_text(
            render_source_page(store, snapshot, page_index)
        )
    (output / "review.html").write_text(render_source_review(store, snapshot))


def ingest_aia(
    *,
    extractor: SourceExtractor,
    output: Path = AIA_OUTPUT,
    source_path: Path = AIA_INPUT,
) -> DocumentSnapshot:
    store = LocalDocumentStore(output, activate_on_publish=False)
    attempts = output / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    attempt_path = attempts / f"{uuid4().hex}.json"
    attempt: dict[str, object] = {
        "started_at_unix": time.time(),
        "source_sha256": AIA_SPEC.sha256,
        "state": "running",
        "manifest_id": None,
    }
    attempt_path.write_text(json.dumps(attempt, indent=2) + "\n")
    try:
        manifest_id = ingest_document(
            source_path.read_bytes(), spec=AIA_SPEC, extractor=extractor, store=store
        )
        snapshot = store.load(manifest_id)
        export_review(store, snapshot)
        store.activate(manifest_id)
    except (ValueError, OSError, RuntimeError) as error:
        attempt.update(state="failed", diagnostic=str(error), completed_at_unix=time.time())
        attempt_path.write_text(json.dumps(attempt, indent=2) + "\n")
        raise
    attempt.update(state="complete", manifest_id=manifest_id, completed_at_unix=time.time())
    attempt_path.write_text(json.dumps(attempt, indent=2) + "\n")
    return snapshot
