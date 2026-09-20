"""Offline generic PDF publication helpers shared by the e2e, catalog and HTTP tests."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

PROVIDER_BASE_URL = "https://provider.invalid"
# Kept short so the authored line fits the 240pt page width (no overflow/truncation).
DOCUMENT_LABEL = "Revenue expense ratio"


def text_partition_sender(calls: list[bytes]) -> Callable[..., bytes]:
    """Classify every page region as offline Text so no semantic model call fires."""

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == f"{PROVIDER_BASE_URL}/v1/chat/completions"
        calls.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        assert "Source text observations:" in prompt
        observations = json.loads(prompt.split("Source text observations:\n", 1)[1])
        span_ids = [str(observation["id"]) for observation in observations]
        # Bind the region to the actual span extents so the literal projection stays
        # in bounds regardless of how the embedded font renders the line.
        bbox = [
            min(float(observation["bbox"][0]) for observation in observations),
            min(float(observation["bbox"][1]) for observation in observations),
            max(float(observation["bbox"][2]) for observation in observations),
            max(float(observation["bbox"][3]) for observation in observations),
        ]
        content: dict[str, object] = {
            "regions": [
                {
                    "region_id": "body",
                    "kind": "Text",
                    "bbox": bbox,
                    "source_span_ids": span_ids,
                    "context_span_ids": [],
                    "list_items": [],
                    "list_ordered": None,
                    "parent_id": None,
                    "interpretation": "Body financial narrative",
                }
            ],
            "unassigned_span_ids": [],
            "diagnostics": [],
        }
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()

    return sender


def ingest_generic_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str = "meridian-semiannual.pdf",
    label: str = DOCUMENT_LABEL,
    page_count: int = 3,
    output_dir: Path | None = None,
) -> tuple[IngestionSummary, list[bytes]]:
    """Ingest an authored PDF through the semantics stage with one stubbed layout call per page."""
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = authored_pdf(tmp_path / filename, page_count=page_count, label=label, embedded_font=True)
    calls: list[bytes] = []
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.json_completion._send_once",
        text_partition_sender(calls),
    )
    summary = ingest_pdf(
        pdf=pdf,
        stage="semantics",
        max_live_calls=page_count,
        output_dir=output_dir if output_dir is not None else tmp_path / "ingestion",
    )
    return summary, calls


def publish_generic_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str,
    label: str,
    page_count: int,
    embedder: EmbeddingPort,
    output_dir: Path | None = None,
) -> DraftPublication:
    """Ingest, qualify, index with the injected embedder and publish one document."""
    ingest, _ = ingest_generic_semantics(
        tmp_path,
        monkeypatch,
        filename=filename,
        label=label,
        page_count=page_count,
        output_dir=output_dir,
    )
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=embedder,
    )
    return publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
