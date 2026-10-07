"""Processing publication cannot hide missing or corrupted dependencies."""

import errno
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.file_placement import sharded_path, stored_names
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from ragspine.extraction.evidence.page.models import (
    CanonicalPage,
    PageInput,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    RetrievalPublication,
    StageOutcome,
    StageState,
)
from ragspine.extraction.evidence.page.service import canonical_page
from tests.enterprise_pdf_rag.adapters.legacy_pointer_helpers import write_pointer
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links


def test_processing_snapshot_reopens_and_corruption_does_not_advance_pointer(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path)
    sources = LocalDocumentStore(tmp_path / "sources")
    pdf = sources.put(b"source", media_type="application/pdf")
    svg = sources.put(b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml")
    text = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        (TextSpan("s0", "Observed", (1.0, 1.0, 2.0, 2.0)),),
    )
    text_ref = sources.put(json.dumps(asdict(text)).encode(), media_type="application/json")
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
    stage = StageOutcome("canonical", "c" * 64, StageState.SUCCEEDED, "observations-v1", canonical)
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
    invalid = replace(manifest, retrieval=RetrievalPublication("f" * 64, missing, missing, ()))
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
    stage = StageOutcome("description", "e" * 64, StageState.SUCCEEDED, "model+prompt-v1", ref)
    store.cache(stage)
    assert store.cached("e" * 64) == stage
    assert store.cached("f" * 64) is None
    store.assets.asset_path(ref).unlink()
    # ADR 0029: an entry whose output is lost is a miss (the stage is recomputed), not an error.
    assert ProcessingStore(tmp_path).cached("e" * 64) is None


def test_content_addressed_put_works_and_stays_idempotent_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    store = LocalDocumentStore(tmp_path)

    first = store.put(b"source", media_type="application/pdf")
    again = store.put(b"source", media_type="application/pdf")

    assert again == first and store.get(first) == b"source"
    assert stored_names(tmp_path / "objects" / "sha256") == [first.sha256]


def test_without_hard_links_a_corrupted_object_is_refused_on_read_and_repaired_by_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"source", media_type="application/pdf")
    store.asset_path(ref).write_bytes(b"tampered")
    forbid_hard_links(monkeypatch)

    with pytest.raises(ValueError, match="digest mismatch"):
        LocalDocumentStore(tmp_path).get(ref)
    # ADR 0029: the caller holds the bytes the digest names, so they replace the damaged copy.
    assert LocalDocumentStore(tmp_path).put(b"source", media_type="application/pdf") == ref

    assert store.asset_path(ref).read_bytes() == b"source"
    assert stored_names(tmp_path / "objects" / "sha256") == [ref.sha256]


def test_a_real_link_failure_still_fails_the_put_and_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch, errno.EACCES)
    store = LocalDocumentStore(tmp_path)

    with pytest.raises(PermissionError):
        store.put(b"source", media_type="application/pdf")

    assert stored_names(tmp_path / "objects" / "sha256") == []


def test_stage_cache_stays_first_writer_wins_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_hard_links(monkeypatch)
    store = ProcessingStore(tmp_path)
    ref = store.assets.put(b"model result", media_type="application/json")
    stage = StageOutcome("description", "e" * 64, StageState.SUCCEEDED, "model+prompt-v1", ref)
    store.cache(stage)
    store.cache(stage)
    assert store.cached("e" * 64) == stage
    pointer = sharded_path(tmp_path / "stage-cache", "e" * 64)
    written = pointer.read_bytes()

    with pytest.raises(ValueError, match="Conflicting immutable stage cache entry"):
        write_pointer(pointer, "d" * 64, immutable=True)

    assert pointer.read_bytes() == written
    assert [path.name for path in pointer.parent.iterdir()] == ["e" * 64]


def test_empty_success_or_failure_cannot_be_a_processing_result() -> None:
    with pytest.raises(ValueError, match="actual artifact"):
        StageOutcome("chart_ir", "a" * 64, StageState.SUCCEEDED, "model-v1")
    with pytest.raises(ValueError, match="diagnostic"):
        StageOutcome("chart_ir", "a" * 64, StageState.UNAVAILABLE, "model-v1")
