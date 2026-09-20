"""The real AIA sample is discovered as a legacy root by id, read-only, without models."""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.processing_retrieval import PROJECTED_CHART_POLICIES
from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.models import ObjectKind
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding

# Only this test names the AIA location; the catalog itself learns it from configuration.
_AIA_PROCESSING = PROCESSING_OUTPUT
pytestmark = pytest.mark.skipif(
    not (_AIA_PROCESSING / "current-processing").is_file(), reason="AIA sample store absent"
)


def _state() -> tuple[bytes, bytes, int, int]:
    source_root = _AIA_PROCESSING.parent
    return (
        (source_root / "current-manifest").read_bytes(),
        (_AIA_PROCESSING / "current-processing").read_bytes(),
        sum(1 for _ in (source_root / "objects" / "sha256").iterdir()),
        sum(1 for _ in (_AIA_PROCESSING / "objects" / "sha256").iterdir()),
    )


def test_aia_sample_is_one_ready_legacy_document_and_stays_untouched(tmp_path: Path) -> None:
    before = _state()
    catalog = scan_catalog(tmp_path / "empty", legacy_roots=(_AIA_PROCESSING,))

    assert catalog.unpublished == ()
    assert len(catalog.documents) == 1
    entry = catalog.documents[0]
    assert entry.retrieval_status == "ready", entry.reason
    assert entry.origin == "legacy"
    outputs = ProcessingStore(_AIA_PROCESSING)
    processing_id, manifest = outputs.load_current()
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    assert entry.document_id == manifest.scope.source_sha256
    assert entry.current_processing_id == processing_id
    assert entry.retrieval_snapshot_id == manifest.retrieval.snapshot_id
    assert entry.member_count == len(plan.members)
    assert entry.embedding_fingerprint is not None
    assert entry.embedding_fingerprint.startswith("local-http/")
    assert entry.selected_physical_pages == manifest.scope.physical_pages
    assert entry.processing_store == str(_AIA_PROCESSING.resolve())
    assert entry.source_store == str(_AIA_PROCESSING.parent.resolve())

    with pytest.raises(ValueError, match="provider"):
        mount_document(entry, embedder=RecordingEmbedding())

    # Evidence-only mount: the lexical corpus is exactly what the snapshot embedded —
    # projected chart text under policy v3, the description alone under older policies.
    texts = mount_document(entry, embedder=None).member_texts()
    assert len(texts) == entry.member_count == len(plan.members)
    charts = [item for item in texts if item.kind is ObjectKind.CHART]
    assert charts
    projected = [item for item in charts if "chart figure" in item.text]
    if plan.qualification_policy in PROJECTED_CHART_POLICIES:
        (donut,) = projected
        assert donut.page_index == 17 and "Agency VONB 72%" in donut.text
    else:
        assert projected == []
    assert _state() == before
