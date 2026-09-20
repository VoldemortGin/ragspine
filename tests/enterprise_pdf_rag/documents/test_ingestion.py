"""A verified source is persisted before an ingestion becomes reviewable."""

from hashlib import sha256

import pytest

from enterprise_pdf_rag.documents.models import DocumentSpec
from enterprise_pdf_rag.documents.service import verify_source


def test_other_pdf_is_rejected_before_any_parser_or_storage_work() -> None:
    spec = DocumentSpec(
        "aia.pdf", sha256(b"selected PDF").hexdigest(), 1, 0, (0.0, 0.0, 100.0, 100.0)
    )
    with pytest.raises(ValueError, match="source SHA-256"):
        verify_source(b"a different PDF", spec)


def test_complete_source_assets_survive_a_store_reopen(tmp_path: object) -> None:
    from pathlib import Path

    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
    from enterprise_pdf_rag.documents.models import (
        DocumentExtraction,
        PageExtraction,
        RegionExtraction,
        TextSpan,
    )
    from enterprise_pdf_rag.documents.service import ingest_document

    assert isinstance(tmp_path, Path)
    source = b"selected PDF"
    spec = DocumentSpec("aia.pdf", sha256(source).hexdigest(), 2, 1, (0.0, 0.0, 100.0, 100.0))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><path d="M0 0L1 1"/></svg>'

    class Extractor:
        def extract_document(self, pdf: bytes) -> DocumentExtraction:
            assert pdf == source
            return DocumentExtraction(
                tuple(
                    PageExtraction(
                        i,
                        100.0,
                        100.0,
                        0,
                        svg,
                        (
                            TextSpan(
                                f"p{i}-s0",
                                f"Source page {i + 1}",
                                (1.0, 1.0, 50.0, 20.0),
                            ),
                        ),
                    )
                    for i in range(2)
                ),
                "test/source-observation",
            )

        def extract_region(
            self,
            pdf: bytes,
            *,
            page_index: int,
            bbox: tuple[float, float, float, float],
        ) -> RegionExtraction:
            return RegionExtraction(page_index, 100.0, 100.0, 0, bbox, svg, svg, ())

    store = LocalDocumentStore(tmp_path)
    manifest_id = ingest_document(source, spec=spec, extractor=Extractor(), store=store)
    reopened = LocalDocumentStore(tmp_path)
    snapshot = reopened.load_current()
    assert snapshot.manifest_id == manifest_id
    assert reopened.get(snapshot.manifest.source) == source
    assert len(snapshot.manifest.pages) == 2
    assert snapshot.manifest.semantics == "pending"
    assert snapshot.manifest.span_to_svg_mapping == "pending"
    assert b"Source page 2" in reopened.get(snapshot.manifest.pages[1].text)
    assert reopened.get(snapshot.manifest.region.native_svg).decode() == svg
    assert ingest_document(source, spec=spec, extractor=Extractor(), store=store) == manifest_id


def test_corrupt_or_missing_object_is_never_reused(tmp_path: object) -> None:
    from pathlib import Path

    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore

    assert isinstance(tmp_path, Path)
    store = LocalDocumentStore(tmp_path)
    ref = store.put(b"original", media_type="application/pdf")
    store.asset_path(ref).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        store.put(b"original", media_type="application/pdf")
    with pytest.raises(ValueError, match="digest mismatch"):
        store.get(ref)
    store.asset_path(ref).unlink()
    with pytest.raises(FileNotFoundError):
        store.get(ref)


def test_non_finite_geometry_cannot_advance_current_manifest(
    prepared_source: tuple[object, DocumentSpec],
) -> None:
    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
    from enterprise_pdf_rag.documents.models import (
        Bounds,
        DocumentExtraction,
        PageExtraction,
        RegionExtraction,
    )
    from enterprise_pdf_rag.documents.service import ingest_document

    store, spec = prepared_source
    assert isinstance(store, LocalDocumentStore)
    previous = store.load_current()
    pdf = store.get(previous.manifest.source)
    svg = store.get(previous.manifest.pages[0].svg).decode()

    class InvalidGeometry:
        def extract_document(self, _pdf: bytes) -> DocumentExtraction:
            return DocumentExtraction((PageExtraction(0, float("inf"), 100.0, 0, svg, ()),), "bad")

        def extract_region(self, _pdf: bytes, *, page_index: int, bbox: Bounds) -> RegionExtraction:
            return RegionExtraction(page_index, float("inf"), 100.0, 0, bbox, svg, svg, ())

    with pytest.raises(ValueError, match="geometry"):
        ingest_document(pdf, spec=spec, extractor=InvalidGeometry(), store=store)
    assert store.load_current().manifest_id == previous.manifest_id
