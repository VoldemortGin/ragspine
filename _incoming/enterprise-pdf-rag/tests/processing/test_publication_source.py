"""A processing pointer cannot activate a source-less or semantically forged closure."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    PageInput,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.service import canonical_page


def test_publication_requires_existing_pinned_source_before_current_changes(
    tmp_path: Path,
) -> None:
    sources = LocalDocumentStore(tmp_path / "sources")
    pdf = sources.put(b"source", media_type="application/pdf")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml"
    )
    text = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        (TextSpan("s0", "Observed", (1.0, 1.0, 2.0, 2.0)),),
    )
    text_ref = sources.put(
        json.dumps(asdict(text)).encode(), media_type="application/json"
    )
    source_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "test.pdf",
            pdf,
            "test",
            (PageRecord(0, 100.0, 100.0, 0, svg, text_ref, 1, ()),),
            RegionRecord(0, (0.0, 0.0, 100.0, 100.0), svg, svg, text_ref, ()),
        )
    )
    store = ProcessingStore(tmp_path / "processed")
    canonical = canonical_page(
        PageInput(source_id, pdf.sha256, 0, 100.0, 100.0, svg, text)
    )
    ref = store.assets.put(
        TypeAdapter(CanonicalPage).dump_json(canonical), media_type="application/json"
    )
    manifest = ProcessingManifest(
        "processing-v1",
        ProcessingScope(source_id, pdf.sha256, 1, (0,)),
        "test",
        (
            PageProcessingRecord(
                0,
                StageOutcome(
                    "canonical",
                    "c" * 64,
                    StageState.SUCCEEDED,
                    "canonical-source-v1",
                    ref,
                ),
                StageOutcome(
                    "partition",
                    "d" * 64,
                    StageState.FAILED,
                    "test",
                    diagnostic="Provider unavailable",
                ),
                (),
            ),
        ),
    )
    snapshot = store.publish(manifest, sources=sources)
    with pytest.raises(FileNotFoundError):
        store.publish(manifest, sources=LocalDocumentStore(tmp_path / "absent"))
    assert (store.root / "current-processing").read_text().strip() == snapshot
