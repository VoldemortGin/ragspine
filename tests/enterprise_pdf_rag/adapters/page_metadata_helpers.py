"""Offline page-metadata stubs: a text-only model reply built from the prompt's own spans."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    PROVIDER_BASE_URL,
    text_partition_sender,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

REGION_VOCABULARY = ("Hong Kong", "Thailand", "Mainland China")
_PERIOD = re.compile(r"\b(?:[12]H\d{2}|FY\d{4})\b")


def metadata_reply(prompt: str, *, fabricate: bool = True) -> dict[str, Any]:
    """Cover on page 1, text elsewhere; title = first span; periods / regions found verbatim.

    ``fabricate`` adds one region that no span prints, which verification must drop.
    """
    page = int(re.search(r"Physical page: (\d+)\.", prompt).group(1))  # type: ignore[union-attr]
    spans: list[dict[str, str]] = json.loads(prompt.split("Source text spans:\n", 1)[1])
    periods = [
        {"text": match.group(0), "span_id": span["id"]}
        for span in spans
        for match in _PERIOD.finditer(span["text"])
    ]
    regions = [
        {"text": region, "span_id": span["id"]}
        for span in spans
        for region in REGION_VOCABULARY
        if region in span["text"]
    ]
    if fabricate:
        regions.append({"text": "Mars", "span_id": spans[0]["id"]})
    return {
        "page_type": "cover" if page == 1 else "text",
        "language": "en",
        "title": {"text": spans[0]["text"], "span_id": spans[0]["id"]},
        "section": None,
        "periods": periods,
        "regions": regions,
    }


def combined_sender(
    calls: list[bytes], *, metadata_calls: list[str], fabricate: bool = True
) -> Callable[..., bytes]:
    """Vision payloads go to the layout stub; text-only payloads get the metadata reply."""
    layout = text_partition_sender(calls)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        content = json.loads(payload)["messages"][1]["content"]
        if isinstance(content, list):
            return layout(url, api_key=api_key, payload=payload, timeout=timeout)
        assert url == f"{PROVIDER_BASE_URL}/v1/chat/completions"
        assert content.startswith("Describe what this one printed page is about")
        metadata_calls.append(content)
        reply = metadata_reply(content, fabricate=fabricate)
        return json.dumps(
            {"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        ).encode()

    return sender


def ingest_with_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str = "meridian-interim.pdf",
    label: str = "Meridian 1H26 Hong Kong",
    page_count: int = 3,
    stage: str = "semantics",
    max_live_calls: int | None = None,
    output_dir: Path | None = None,
) -> tuple[IngestionSummary, list[bytes], list[str]]:
    """Ingest an authored PDF with stubbed layout and metadata calls; budget covers both."""
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = authored_pdf(tmp_path / filename, page_count=page_count, label=label, embedded_font=True)
    calls: list[bytes] = []
    metadata_calls: list[str] = []
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.json_completion._send_once",
        combined_sender(calls, metadata_calls=metadata_calls),
    )
    summary = ingest_pdf(
        pdf=pdf,
        stage=stage,  # type: ignore[arg-type]
        max_live_calls=2 * page_count if max_live_calls is None else max_live_calls,
        output_dir=output_dir if output_dir is not None else tmp_path / "ingestion",
    )
    return summary, calls, metadata_calls


def publish_with_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str,
    label: str,
    page_count: int,
    output_dir: Path,
    embedder: EmbeddingPort | None = None,
) -> DraftPublication:
    ingest, _, _ = ingest_with_metadata(
        tmp_path,
        monkeypatch,
        filename=filename,
        label=label,
        page_count=page_count,
        output_dir=output_dir,
    )
    indexed = index_draft(
        source_store=Path(ingest.source_store),
        processing_store=Path(ingest.processing_store),
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder() if embedder is None else embedder,
    )
    return publish_draft(
        source_store=Path(ingest.source_store),
        processing_store=Path(ingest.processing_store),
        processing_id=indexed.indexed_processing_id,
    )
