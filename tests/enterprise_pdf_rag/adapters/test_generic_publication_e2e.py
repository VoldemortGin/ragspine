"""Generic PDF ingest→qualify→index→publish→retrieve runs fully offline, no models."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pytest import CaptureFixture

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.cli import main
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

_PROVIDER_BASE_URL = "https://provider.invalid"
# Kept short so the authored line fits the 240pt page width (no overflow/truncation).
_DOCUMENT_LABEL = "Revenue expense ratio"
_QUERY = "revenue expense ratio"


def _text_partition_sender(calls: list[bytes]) -> Callable[..., bytes]:
    """Classify every page region as offline Text so no semantic model call fires."""

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == f"{_PROVIDER_BASE_URL}/v1/chat/completions"
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


def _ingest_generic_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[IngestionSummary, list[bytes]]:
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": _PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = authored_pdf(
        tmp_path / "meridian-semiannual.pdf",
        page_count=3,
        label=_DOCUMENT_LABEL,
        embedded_font=True,
    )
    calls: list[bytes] = []
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.json_completion._send_once",
        _text_partition_sender(calls),
    )
    summary = ingest_pdf(
        pdf=pdf,
        stage="semantics",
        max_live_calls=3,
        output_dir=tmp_path / "ingestion",
    )
    return summary, calls


def test_generic_pdf_ingest_qualify_index_publish_retrieve_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = _ingest_generic_semantics(tmp_path, monkeypatch)

    assert ingest.indexed is False
    assert ingest.activated is False
    assert ingest.retrieval_status.startswith("not_ready")
    assert ingest.failed_stage_count == 0
    assert ingest.live_call_count == 3
    assert len(calls) == 3  # one layout call per page; Text needs no semantic model call

    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    sources = LocalDocumentStore(source_store)
    outputs = ProcessingStore(processing_store)
    assert not (outputs.root / "current-processing").exists()
    assert not (sources.root / "current-manifest").exists()

    qualified = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert qualified.eligible_member_count >= 2
    assert qualified.retrieval_status == "qualified; indexing pending"
    assert qualified.indexed is False and qualified.activated is False

    embedder = OfflineDescriptionEmbedder()
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=embedder,
    )
    assert indexed.indexed is True and indexed.activated is False
    assert indexed.indexed_processing_id != ingest.processing_id
    assert indexed.member_count == qualified.eligible_member_count
    assert indexed.embedding_dimensions == (64,)
    assert indexed.retrieval_status == "indexed; publication pending"
    assert not (outputs.root / "current-processing").exists()

    published = publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    assert published.retrieval_status == "ready"
    assert published.indexed is True and published.activated is True
    assert published.published_processing_id == indexed.indexed_processing_id
    assert published.retrieval_snapshot_id == indexed.retrieval_snapshot_id
    assert published.source_manifest_id == ingest.source_manifest_id

    current_id, current_manifest = outputs.load_current()
    assert current_id == published.published_processing_id
    publication = current_manifest.retrieval
    assert publication is not None
    assert sources.load_current().manifest_id == ingest.source_manifest_id
    assert (sources.root / "current-manifest").read_text().strip() == ingest.source_manifest_id

    retrieval = ProcessingRetrieval(sources, outputs, embedder)
    hits = retrieval.search(publication, _QUERY)
    assert hits
    top = hits[0]
    assert top.snapshot_id == published.retrieval_snapshot_id
    assert top.member_id

    plan, _ = outputs.load_retrieval(publication)
    assert plan.scope.source_manifest_id == ingest.source_manifest_id
    assert top.member_id in {member.member_id for member in plan.members}

    context = retrieval.resolve(publication, top)
    assert context.snapshot_id == top.snapshot_id
    assert context.member.member_id == top.member_id

    # Only the three per-page layout calls ever reached a provider seam.
    assert len(calls) == 3


def test_generic_pdf_cli_qualify_index_publish_smoke(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = _ingest_generic_semantics(tmp_path, monkeypatch)
    assert ingest.retrieval_status.startswith("not_ready")

    processing_store = Path(ingest.processing_store)
    common = [
        "--source-store",
        str(Path(ingest.source_store)),
        "--processing-store",
        str(processing_store),
    ]

    assert main(["qualify", *common, "--processing-id", ingest.processing_id]) == 0
    qualify_payload = json.loads(capsys.readouterr().out)
    assert qualify_payload["processing_id"] == ingest.processing_id
    assert qualify_payload["eligible_member_count"] >= 2
    assert qualify_payload["retrieval_status"] == "qualified; indexing pending"
    assert qualify_payload["indexed"] is False and qualify_payload["activated"] is False

    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("EMBEDDING_MODEL", "offline-test-model")
    monkeypatch.setenv("EMBEDDING_API_KEY", "offline-test-key")
    monkeypatch.setattr(
        "enterprise_pdf_rag.cli.LocalEmbeddingAdapter",
        lambda config: OfflineDescriptionEmbedder(),
    )
    assert main(["index", *common, "--processing-id", ingest.processing_id]) == 0
    index_payload = json.loads(capsys.readouterr().out)
    assert index_payload["processing_id"] == ingest.processing_id
    indexed_id = str(index_payload["indexed_processing_id"])
    assert indexed_id != ingest.processing_id
    assert index_payload["retrieval_status"] == "indexed; publication pending"
    assert index_payload["indexed"] is True and index_payload["activated"] is False

    assert main(["publish", *common, "--processing-id", indexed_id]) == 0
    publish_payload = json.loads(capsys.readouterr().out)
    assert publish_payload["published_processing_id"] == indexed_id
    assert publish_payload["current_processing_id"] == indexed_id
    assert publish_payload["retrieval_status"] == "ready"
    assert publish_payload["indexed"] is True and publish_payload["activated"] is True
    assert publish_payload["source_activated"] is True

    # not_ready → qualified → indexed → ready, one processing lineage, zero model calls.
    assert (processing_store / "current-processing").read_text().strip() == indexed_id
    assert len(calls) == 3
