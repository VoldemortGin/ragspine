"""Small explicit source observations; these are not the live AIA corpus."""

from hashlib import sha256
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.documents.models import (
    Bounds,
    DocumentExtraction,
    DocumentSpec,
    PageExtraction,
    RegionExtraction,
    TextSpan,
)
from enterprise_pdf_rag.documents.service import ingest_document


@pytest.fixture
def prepared_source(tmp_path: Path) -> tuple[LocalDocumentStore, DocumentSpec]:
    source = b"test-only selected source bytes"
    spec = DocumentSpec(
        "selected-source.pdf",
        sha256(source).hexdigest(),
        1,
        0,
        (0.0, 0.0, 100.0, 100.0),
    )
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><path d="M0 0L1 1"/></svg>'
    span = TextSpan("page-0-span-0", "Observed source title", (1.0, 1.0, 90.0, 20.0))

    class Extractor:
        def extract_document(self, pdf: bytes) -> DocumentExtraction:
            assert pdf == source
            return DocumentExtraction(
                (PageExtraction(0, 100.0, 100.0, 0, svg, (span,)),),
                "test/source-observations",
            )

        def extract_region(
            self, pdf: bytes, *, page_index: int, bbox: Bounds
        ) -> RegionExtraction:
            assert pdf == source
            return RegionExtraction(
                page_index, 100.0, 100.0, 0, bbox, svg, svg, (span,)
            )

    store = LocalDocumentStore(tmp_path)
    ingest_document(source, spec=spec, extractor=Extractor(), store=store)
    return store, spec
