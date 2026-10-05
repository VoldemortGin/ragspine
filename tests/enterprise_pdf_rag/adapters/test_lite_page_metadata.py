"""Lite ingest (ADR 0025): page metadata derived from the page itself, no model call."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.page_metadata_extraction import PAGE_METADATA_DETERMINISTIC
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.metadata.page_metadata import PageMetadata, PageType
from ragspine.extraction.evidence.page.models import StageState
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    RUNNING_HEADER,
    TITLE,
    artifact_of,
    lite_env,
    mixed_folder,
    processing_store_of,
    published_manifest,
    run_mode,
)

# ---- 2. deterministic page metadata ------------------------------------------------------------


def test_lite_page_metadata_is_deterministic_verbatim_and_feeds_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)

    result = run_mode(tmp_path, "lite")

    manifest = published_manifest(result)
    outputs = ProcessingStore(processing_store_of(tmp_path, "ingestion"))
    pages = []
    for page in manifest.pages:
        assert page.metadata is not None and page.metadata.state is StageState.SUCCEEDED
        assert page.metadata.producer == PAGE_METADATA_DETERMINISTIC
        pages.append(
            TypeAdapter(PageMetadata).validate_json(outputs.assets.get(artifact_of(page.metadata)))
        )
    first = pages[0]
    assert first.title is not None and first.title.text == TITLE
    assert all(page.section is not None and page.section.text == RUNNING_HEADER for page in pages)
    assert "1H2026" in first.normalized_periods
    assert all(page.regions == () and page.language is None for page in pages)
    assert all(page.page_type is PageType.OTHER for page in pages)
    (document,) = result.documents
    assert document.ingestion is not None and document.ingestion.display_title == TITLE
    contexts = outputs.index_contexts(manifest)
    assert contexts[0].header() == f"{TITLE} | {TITLE} | {RUNNING_HEADER}"
