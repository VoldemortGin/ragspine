"""Offline generic PDF publication helpers shared by the e2e, catalog and HTTP tests."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_retrieval import resolve_processing_context
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from enterprise_pdf_rag.processing.models import ObjectKind
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    DEFAULT_TABLE,
    TABLE_COLUMNS,
    TABLE_ROWS,
    TableSpec,
    authored_pdf,
)

PROVIDER_BASE_URL = "https://provider.invalid"
# Kept short so the authored line fits the 240pt page width (no overflow/truncation).
DOCUMENT_LABEL = "Revenue expense ratio"
# The ruled grid of ``authored_pdf(table_page=True)``.
TABLE_BBOX = (TABLE_COLUMNS[0], TABLE_ROWS[0], TABLE_COLUMNS[-1], TABLE_ROWS[-1])


def table_region(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The layout region the stub partitioner proposes around a ruled grid (5pt margin)."""
    return (bbox[0] - 5.0, bbox[1] - 5.0, bbox[2] + 5.0, bbox[3] + 5.0)


def _spec(table_page: bool | TableSpec) -> TableSpec | None:
    if isinstance(table_page, TableSpec):
        return table_page
    return DEFAULT_TABLE if table_page else None


def _extent(observations: list[dict[str, Any]]) -> list[float]:
    # Bind the region to the actual span extents so the literal projection stays
    # in bounds regardless of how the embedded font renders the line. A real layout
    # model echoes the prompt's coordinates in their shortest decimal form
    # (``42.400000000000006`` becomes ``42.4``), so the stub does the same.
    boxes = [
        [round(float(value), 6) for value in observation["bbox"]] for observation in observations
    ]
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _region(region_id: str, kind: str, bbox: list[float], span_ids: list[str]) -> dict[str, object]:
    return {
        "region_id": region_id,
        "kind": kind,
        "bbox": bbox,
        "source_span_ids": span_ids,
        "context_span_ids": [],
        "list_items": [],
        "list_ordered": None,
        "parent_id": None,
        "interpretation": "Body financial narrative" if kind == "Text" else "Ruled metrics table",
    }


def text_partition_sender(
    calls: list[bytes],
    *,
    table_caption: bool = False,
    table_bbox: tuple[float, float, float, float] = TABLE_BBOX,
) -> Callable[..., bytes]:
    """Classify page regions offline: occurrences inside the authored ruled grid become one
    Table region, everything else one Text region, so no semantic model call fires.

    ``table_caption`` also hands the page's caption line to the Table region, which
    leaves the region owning an occurrence outside its native cells. ``table_bbox`` is the
    authored grid to classify against, for fixtures other than the default one.
    """
    region = table_region(table_bbox)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == f"{PROVIDER_BASE_URL}/v1/chat/completions"
        calls.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        assert "Source text observations:" in prompt
        observations: list[dict[str, Any]] = json.loads(
            prompt.split("Source text observations:\n", 1)[1]
        )
        in_grid = [
            observation
            for observation in observations
            if table_bbox[0] <= float(observation["bbox"][0])
            and table_bbox[1] <= float(observation["bbox"][1])
            and float(observation["bbox"][2]) <= table_bbox[2]
            and float(observation["bbox"][3]) <= table_bbox[3]
        ]
        outside = [observation for observation in observations if observation not in in_grid]
        regions: list[dict[str, object]] = []
        if in_grid:
            owned = in_grid + (outside if table_caption else [])
            extent = _extent(owned)
            bbox = [
                min(region[0], extent[0]),
                min(region[1], extent[1]),
                max(region[2], extent[2]),
                max(region[3], extent[3]),
            ]
            regions.append(_region("table", "Table", bbox, [str(item["id"]) for item in owned]))
            if table_caption:
                outside = []
        if outside:
            regions.append(
                _region("body", "Text", _extent(outside), [str(item["id"]) for item in outside])
            )
        content: dict[str, object] = {
            "regions": regions,
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
    table_page: bool | TableSpec = False,
    table_caption: bool = False,
) -> tuple[IngestionSummary, list[bytes]]:
    """Ingest an authored PDF through the semantics stage with one stubbed layout call per page.

    ``table_page`` draws a native ruled table on the last page (``True`` for the default
    grid, or a ``TableSpec``); ``table_caption`` makes the stub layout hand that page's
    caption line to the Table region as well.
    """
    spec = _spec(table_page)
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = authored_pdf(
        tmp_path / filename,
        page_count=page_count,
        label=label,
        embedded_font=True,
        table_page=table_page,
    )
    calls: list[bytes] = []
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.json_completion._send_once",
        text_partition_sender(
            calls,
            table_caption=table_caption,
            table_bbox=spec.bbox if spec is not None else TABLE_BBOX,
        ),
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
    table_page: bool | TableSpec = False,
) -> DraftPublication:
    """Ingest, qualify, index with the injected embedder and publish one document."""
    ingest, _ = ingest_generic_semantics(
        tmp_path,
        monkeypatch,
        filename=filename,
        label=label,
        page_count=page_count,
        output_dir=output_dir,
        table_page=table_page,
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


def resolve_table_member(published: DraftPublication) -> RetrievalContext:
    """Hydrate the single published Table member through the real stores; no model call."""
    sources = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(published.processing_store))
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.TABLE)
    hit = PinnedRetrievalHit(plan.snapshot_id, member.member_id, 1.0)
    return resolve_processing_context(sources, outputs, manifest.retrieval, hit)
