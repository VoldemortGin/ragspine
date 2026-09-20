"""Processing publication cannot hide missing or corrupted dependencies."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import (
    AssetRef,
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
    RetrievalPublication,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.service import canonical_page


def test_processing_snapshot_reopens_and_corruption_does_not_advance_pointer(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path)
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
    canonical = store.assets.put(
        TypeAdapter(CanonicalPage).dump_json(
            canonical_page(PageInput(source_id, pdf.sha256, 0, 100.0, 100.0, svg, text))
        ),
        media_type="application/json",
    )
    stage = StageOutcome(
        "canonical", "c" * 64, StageState.SUCCEEDED, "observations-v1", canonical
    )
    failed = StageOutcome(
        "partition",
        "d" * 64,
        StageState.FAILED,
        "layout-model-v1",
        diagnostic="Provider unavailable; no synthetic partition fallback",
    )
    manifest = ProcessingManifest(
        "processing-v1",
        ProcessingScope(source_id, pdf.sha256, 1, (0,)),
        "processor-v1",
        (PageProcessingRecord(0, stage, failed, ()),),
    )
    snapshot_id = store.publish(manifest, sources=sources)
    reopened = ProcessingStore(tmp_path)
    assert reopened.load_current() == (snapshot_id, manifest)
    assert reopened.load(snapshot_id) == manifest
    missing = AssetRef("f" * 64, "application/json", 1)
    invalid = replace(
        manifest, retrieval=RetrievalPublication("f" * 64, missing, missing, ())
    )
    with pytest.raises(FileNotFoundError):
        reopened.publish(invalid, sources=sources)
    assert (tmp_path / "current-processing").read_text().strip() == snapshot_id
    reopened.assets.asset_path(canonical).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        reopened.publish(manifest, sources=sources)
    assert (tmp_path / "current-processing").read_text().strip() == snapshot_id


def test_stage_cache_is_bound_to_input_and_validates_its_actual_output(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path)
    ref = store.assets.put(b"model result", media_type="application/json")
    stage = StageOutcome(
        "description", "e" * 64, StageState.SUCCEEDED, "model+prompt-v1", ref
    )
    store.cache(stage)
    assert store.cached("e" * 64) == stage
    assert store.cached("f" * 64) is None
    store.assets.asset_path(ref).unlink()
    with pytest.raises(FileNotFoundError):
        store.cached("e" * 64)


def test_empty_success_or_failure_cannot_be_a_processing_result() -> None:
    with pytest.raises(ValueError, match="actual artifact"):
        StageOutcome("chart_ir", "a" * 64, StageState.SUCCEEDED, "model-v1")
    with pytest.raises(ValueError, match="diagnostic"):
        StageOutcome("chart_ir", "a" * 64, StageState.UNAVAILABLE, "model-v1")
