"""Selected processing cannot use the old source manifest's page-25 focus region."""

import json
from dataclasses import asdict
from pathlib import Path

from enterprise_pdf_rag.adapters.aia_processing import ProcessingPipeline
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
    LayoutObject,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
)


def test_cached_source_processes_only_first_twenty_and_records_failure(
    tmp_path: Path,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    pdf = sources.put(b"unit-test source", media_type="application/pdf")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg"/>', media_type="image/svg+xml"
    )
    pages = []
    for index in range(71):
        text = TextSidecar(
            "source-text-v1",
            pdf.sha256,
            index,
            (TextSpan(f"p{index}-s0", f"Page {index + 1}", (1.0, 1.0, 20.0, 10.0)),),
        )
        sidecar = sources.put(
            json.dumps(asdict(text)).encode(), media_type="application/json"
        )
        pages.append(PageRecord(index, 960.0, 540.0, 0, svg, sidecar, 1, ()))
    manifest = DocumentManifest(
        "source-ingestion-v1",
        "unit.pdf",
        pdf,
        "test-source",
        tuple(pages),
        RegionRecord(24, (0.0, 0.0, 100.0, 100.0), svg, svg, pages[24].text, ()),
    )
    source_id = sources.publish(manifest)
    called: list[int] = []

    class Partition:
        fingerprint = "layout-v1"

        def partition(self, page: PageInput) -> PagePartition:
            called.append(page.page_index)
            if page.page_index == 3:
                raise ValueError("test provider unavailable")
            return PagePartition(
                "layout-v1",
                page.source_manifest_id,
                page.source_sha256,
                page.page_index,
                self.fingerprint,
                (),
                tuple(s.span_id for s in page.text.spans),
                ("No inferred regions in test result",),
            )

    class Objects:
        def process(
            self, _page: PageInput, _item: LayoutObject
        ) -> ObjectProcessingRecord:
            raise AssertionError("No objects expected from this test partition")

    outputs = ProcessingStore(tmp_path / "processing")
    pipeline = ProcessingPipeline(sources, outputs, Partition(), Objects())
    first_id, first = pipeline.run(source_id, selected_page_indices=tuple(range(20)))
    assert called == list(range(20))
    assert first.scope.source_page_count == 71
    assert first.pages[3].partition.diagnostic == "test provider unavailable"
    assert not first.pages[3].objects
    assert [record.page_index for record in first.pages] == list(range(20))
    assert first.pages[0].raw_partition is not None
    assert first.pages[0].raw_partition.artifact != first.pages[0].partition.artifact
    assert first.pages[0].partition.producer.startswith("layout-normalization-")
    assert outputs.load(first_id) == first
    called.clear()
    second_id, second = pipeline.run(source_id, selected_page_indices=tuple(range(20)))
    assert called == [
        3
    ]  # Successful layout outputs are reused; the failure is explicit.
    assert second_id == first_id and second == first
