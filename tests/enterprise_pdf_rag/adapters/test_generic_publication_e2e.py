"""Generic PDF ingest→qualify→index→publish→retrieve runs fully offline, no models."""

import json
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
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.figures.models import Verification
from enterprise_pdf_rag.processing.context_builder import BlockKind, build_context_block
from enterprise_pdf_rag.processing.models import ObjectKind, StageState
from enterprise_pdf_rag.processing.table_models import TableIR
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)

_QUERY = "revenue expense ratio"


def test_generic_pdf_ingest_qualify_index_publish_retrieve_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch)

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
    ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch)
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


class _RecordingOfflineEmbedder(OfflineDescriptionEmbedder):
    """The offline vector plus a record of exactly which texts were embedded."""

    def __init__(self) -> None:
        self.descriptions: list[str] = []

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.descriptions.append(text)
        return super().embed_description(text)


def test_generic_pdf_native_table_is_indexed_description_only_under_policy_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch, table_page=True)
    assert ingest.failed_stage_count == 0 and len(calls) == 3
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)

    qualified = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert qualified.qualification_policy == "retrieval-eligibility-kind-and-stage-completeness-v2"
    assert qualified.kinds == {"Table": 1, "Text": 3}
    assert qualified.skipped_reasons == {}

    embedder = _RecordingOfflineEmbedder()
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=embedder,
    )
    assert indexed.member_count == 4
    published = publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    sources = LocalDocumentStore(source_store)
    outputs = ProcessingStore(processing_store)
    _, manifest = outputs.load_current()
    publication = manifest.retrieval
    assert publication is not None
    plan, _ = outputs.load_retrieval(publication)
    assert plan.qualification_policy == "source-transcription-and-scoped-chart-qualification-v2"
    (table_member,) = tuple(member for member in plan.members if member.kind is ObjectKind.TABLE)
    assert table_member.page_index == 2

    # Description-only: the embedded text is the cells' source text, never the grid.
    table_text = "Metric\nValue\nRevenue\n1,234\nMargin"
    assert table_text in embedder.descriptions
    assert not any("cells." in text or "row" in text for text in embedder.descriptions)

    retrieval = ProcessingRetrieval(sources, outputs, embedder)
    hits = retrieval.search(publication, "Metric Value Margin 1,234", limit=4)
    assert hits[0].member_id == table_member.member_id
    assert hits[0].snapshot_id == published.retrieval_snapshot_id

    context = retrieval.resolve(publication, hits[0])
    assert isinstance(context.ir, TableIR)
    assert context.ir.verification is Verification.PENDING  # inferred grid stays pending
    assert context.description.verification is Verification.VERIFIED
    assert context.description.text == table_text
    assert context.scope == "literal-source-transcription-v1"
    assert (context.ir.row_count, context.ir.col_count) == (3, 2)
    assert tuple(cell.text for cell in context.ir.cells) == (*table_text.split("\n"), "")
    block = build_context_block(context)
    assert block.kind is BlockKind.TABLE and block.verification is Verification.VERIFIED
    value = next(cell for cell in block.cells if cell.text == "1,234")
    assert (value.row, value.col) == (1, 1) and len(value.source_span_ids) == 1
    assert f"cells.{value.cell_id} (1,1): 1,234" in block.prompt_text()
    assert len(calls) == 3


def test_generic_pdf_table_owning_a_caption_is_not_verified_and_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _ = ingest_generic_semantics(tmp_path, monkeypatch, table_page=True, table_caption=True)
    assert ingest.failed_stage_count == 0
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    manifest = ProcessingStore(processing_store).load(ingest.processing_id)
    (record,) = tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.TABLE
    )
    stages = {stage.stage: stage for stage in record.stages}
    assert stages["ir"].state is StageState.SUCCEEDED  # the grid itself was observed
    assert stages["description"].state is StageState.UNAVAILABLE
    assert stages["qualification"].state is StageState.UNAVAILABLE
    assert "outside its native cells" in (stages["qualification"].diagnostic or "")

    qualified = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert qualified.kinds == {"Text": 2}
    assert qualified.skipped_reasons == {
        "Table transcription is not verified; only verified tables are retrievable": 1
    }
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert indexed.member_count == 2
