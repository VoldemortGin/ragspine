"""A persisted authored bar and literal footer before numeric admission."""

from pathlib import Path

from pydantic import TypeAdapter
from tests.adapters.bar_source_fixture import BarSource
from tests.adapters.test_bar_publication import publication_input
from tests.processing.test_persistent_retrieval import RecordingEmbedding

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    LayoutObject,
    ObjectKind,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.service import canonical_page


def published_bar_input(
    tmp_path: Path,
) -> tuple[LocalDocumentStore, ProcessingStore, str, LayoutObject, BarSource]:
    sources, assets, scope, item, record, source = publication_input(tmp_path)
    outputs = ProcessingStore(assets.root)
    footer_spans = tuple(
        span
        for span in source.page.text.spans
        if span.span_id not in item.source_span_ids
    )
    assert len(footer_spans) == 1
    footer = LayoutObject(
        "authored-footer",
        ObjectKind.TEXT,
        footer_spans[0].bbox,
        tuple(span.span_id for span in footer_spans),
        "literal footer",
        Confidence(None, "source transcription"),
    )
    footer_record = ProcessingObjectAdapter(sources, outputs).process(
        source.page, footer
    )
    publication = ProcessingRetrieval(sources, outputs, RecordingEmbedding()).build(
        scope, ((0, record), (0, footer_record))
    )
    canonical = outputs.assets.put(
        TypeAdapter(CanonicalPage).dump_json(canonical_page(source.page)),
        media_type="application/json",
    )
    partition = outputs.assets.put(
        TypeAdapter(PagePartition).dump_json(
            PagePartition(
                "layout-v2",
                scope.source_manifest_id,
                scope.source_sha256,
                0,
                "authored",
                (item, footer),
                (),
            )
        ),
        media_type="application/json",
    )
    manifest = ProcessingManifest(
        "processing-v1",
        scope,
        "authored",
        (
            PageProcessingRecord(
                0,
                StageOutcome(
                    "canonical", "a" * 64, StageState.SUCCEEDED, "test", canonical
                ),
                StageOutcome(
                    "partition", "b" * 64, StageState.SUCCEEDED, "test", partition
                ),
                (record, footer_record),
            ),
        ),
        publication,
    )
    return sources, outputs, outputs.publish(manifest, sources=sources), item, source
