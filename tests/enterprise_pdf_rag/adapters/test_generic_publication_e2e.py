"""Generic PDF ingest→qualify→index→publish→retrieve runs fully offline, no models."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from pydantic import TypeAdapter
from pytest import CaptureFixture

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.literal_qualification import _reprove_table_grid
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.models import AbstainReason, AnswerStatus, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.verify import decide, verify_claims
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.processing.context_builder import BlockKind, build_context_block
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.objects.tables.table_grid_proof import (
    GRID_SCOPE,
    strip_grid_evidence,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, LiteralQualification
from ragspine.extraction.evidence.page.models import ObjectKind, StageState
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    DIAGRAM_DESCRIPTION,
    DOCUMENT_LABEL,
    ingest_generic_semantics,
    publish_generic_document,
    resolve_table_member,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import MULTI_HEADER_TABLE

_QUERY = "revenue expense ratio"


def _no_chart(member_id: str) -> Never:
    raise AssertionError("a diagram claim must never read chart evidence")


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
    assert plan.qualification_policy == "source-transcription-and-scoped-chart-qualification-v5"
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
    assert context.ir.verification is Verification.VERIFIED  # ADR 0014: the grid is ruled
    assert context.description.verification is Verification.VERIFIED
    assert context.description.text == table_text
    assert context.scope == "literal-source-transcription-v1"
    assert (context.ir.row_count, context.ir.col_count) == (3, 2)
    assert tuple(cell.text for cell in context.ir.cells) == (*table_text.split("\n"), "")
    block = build_context_block(context)
    assert block.kind is BlockKind.TABLE and block.verification is Verification.VERIFIED
    assert block.grid_verification is Verification.VERIFIED
    value = next(cell for cell in block.cells if cell.text == "1,234")
    assert (value.row, value.col) == (1, 1) and len(value.source_span_ids) == 1
    # All rules are 1pt, so the grid proves but no header band does.
    assert f"cells.{value.cell_id} (1,1): 1,234 row=1 col=1 header=<NONE>" in block.prompt_text()
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
    assert (stages["qualification"].diagnostic or "").endswith("grid=verified")
    # The two branches are independent facts: the ruled grid proves even though the
    # caption kept the verbatim transcription from qualifying.
    artifact = stages["ir"].artifact
    assert artifact is not None
    observed = TypeAdapter(TableIR).validate_json(
        ProcessingStore(processing_store).assets.get(artifact)
    )
    assert observed.verification is Verification.VERIFIED

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


def test_generic_pdf_diagram_is_proven_indexed_and_cited_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls = ingest_generic_semantics(tmp_path, monkeypatch, diagram_page=True)
    # Three layout calls plus the two visual branches of the single Diagram object.
    assert ingest.failed_stage_count == 0 and len(calls) == 5
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)

    manifest = ProcessingStore(processing_store).load(ingest.processing_id)
    (record,) = tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.DIAGRAM
    )
    stages = {stage.stage: stage for stage in record.stages}
    assert all(
        stages[name].state is StageState.SUCCEEDED
        for name in ("qualified_ir", "qualified_description", "qualification")
    )
    assert record.qualified_claim_count == 3  # two proven nodes and one proven edge

    qualified = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert qualified.kinds == {"Diagram": 1, "Text": 3}
    assert qualified.skipped_reasons == {}

    embedder = _RecordingOfflineEmbedder()
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=embedder,
    )
    assert indexed.member_count == 4
    projection = "diagram figure PLAN BUILD PLAN -> BUILD"
    assert any(text.split("\n")[-1] == projection for text in embedder.descriptions)
    assert not any(DIAGRAM_DESCRIPTION in text for text in embedder.descriptions)

    published = publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    sources = LocalDocumentStore(source_store)
    outputs = ProcessingStore(processing_store)
    _, current = outputs.load_current()
    publication = current.retrieval
    assert publication is not None
    plan, _ = outputs.load_retrieval(publication)
    assert plan.qualification_policy == "source-transcription-and-scoped-chart-qualification-v5"
    (diagram_member,) = tuple(
        member for member in plan.members if member.kind is ObjectKind.DIAGRAM
    )

    retrieval = ProcessingRetrieval(sources, outputs, embedder)
    hits = retrieval.search(publication, "PLAN BUILD", limit=4)
    assert hits[0].member_id == diagram_member.member_id
    assert hits[0].snapshot_id == published.retrieval_snapshot_id

    context = retrieval.resolve(publication, hits[0])
    assert isinstance(context.ir, DiagramIR)
    assert context.ir.verification is Verification.VERIFIED
    assert context.scope == "diagram-structure-source-geometry-v1"
    assert (
        context.description.text == "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."
    )

    block = build_context_block(context)
    assert block.kind is BlockKind.DIAGRAM
    rendered = block.prompt_text()
    assert "diagram nodes=2 edges=1" in rendered
    assert "nodes.n1.label: PLAN" in rendered and "edges.0: PLAN -> BUILD" in rendered

    # The answer chain, without a model: a printed edge is citable, its reverse is not.
    blocks = {block.member_id: block}
    answer = ModelAnswer(
        abstain=False,
        abstain_reason=None,
        answer="After PLAN comes BUILD.",
        claims=(
            ModelClaim(
                claim_id="c1",
                member_id=block.member_id,
                kind="diagram_edge",
                field_path="edges.0",
                text="PLAN -> BUILD",
            ),
        ),
    )
    verification = verify_claims(answer, blocks, chart_evidence=_no_chart)
    (verified,) = verification.verified
    assert verification.rejected == () and verified.kind is ClaimKind.DIAGRAM_EDGE
    assert decide(answer, verification, blocks_present=True) == (AnswerStatus.ANSWERED, None, None)

    reversed_claim = ModelAnswer(
        abstain=False,
        abstain_reason=None,
        answer="After BUILD comes PLAN.",
        claims=(
            ModelClaim(
                claim_id="c1",
                member_id=block.member_id,
                kind="diagram_edge",
                field_path="edges.0",
                text="BUILD -> PLAN",
            ),
        ),
    )
    (rejected,) = verify_claims(reversed_claim, blocks, chart_evidence=_no_chart).rejected
    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert len(calls) == 5


def test_generic_pdf_diagram_owning_a_caption_is_not_proven_and_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _ = ingest_generic_semantics(
        tmp_path, monkeypatch, diagram_page=True, diagram_caption=True
    )
    assert ingest.failed_stage_count == 0
    source_store = Path(ingest.source_store)
    processing_store = Path(ingest.processing_store)
    manifest = ProcessingStore(processing_store).load(ingest.processing_id)
    (record,) = tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.DIAGRAM
    )
    stages = {stage.stage: stage for stage in record.stages}
    assert stages["ir"].state is StageState.SUCCEEDED  # the structure itself was inferred
    assert "qualified_ir" not in stages and "qualified_description" not in stages
    assert stages["qualification"].state is StageState.UNAVAILABLE
    assert (stages["qualification"].diagnostic or "").startswith("object: uncited_source_span:")

    qualified = qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    assert qualified.kinds == {"Text": 2}
    assert qualified.skipped_reasons == {
        "Diagram structure is not proven; only geometry-qualified diagrams are retrievable": 1
    }
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert indexed.member_count == 2


def test_generic_pdf_table_grid_receipt_must_bind_the_proved_rulings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-semiannual.pdf",
        label=DOCUMENT_LABEL,
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
        table_page=True,
    )
    context = resolve_table_member(published)
    table, receipt = context.ir, context.qualification
    assert isinstance(table, TableIR) and table.grid_evidence is not None
    assert isinstance(receipt, LiteralQualification)
    assert (receipt.grid_scope, receipt.ruling_digest) == (
        GRID_SCOPE,
        table.grid_evidence.ruling_digest,
    )

    sources = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    source = sources.load(receipt.source_manifest_id)
    page_index = context.member.page_index
    pdf = sources.get(source.manifest.source)
    spans = read_text_sidecar(sources, source, page_index).spans
    _reprove_table_grid(pdf, table, receipt, page_index=page_index, spans=spans)

    for tampered in (replace(receipt, ruling_digest="0" * 64), replace(receipt, grid_scope=None)):
        with pytest.raises(ValueError, match="does not bind the proved rulings"):
            _reprove_table_grid(pdf, table, tampered, page_index=page_index, spans=spans)
    with pytest.raises(ValueError, match="must not carry a grid qualification"):
        _reprove_table_grid(
            pdf, strip_grid_evidence(table), receipt, page_index=page_index, spans=spans
        )


def test_generic_pdf_multi_header_table_publishes_with_proved_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-semiannual.pdf",
        label=DOCUMENT_LABEL,
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
        table_page=MULTI_HEADER_TABLE,
    )
    context = resolve_table_member(published)
    table = context.ir
    assert isinstance(table, TableIR) and table.grid_evidence is not None
    assert table.verification is Verification.VERIFIED
    # The 2pt rule under row 1 closes a two-row header; both rows are proved.
    assert table.grid_evidence.proved_header_rows() == frozenset({0, 1})

    block = build_context_block(context)
    value = next(cell for cell in block.cells if (cell.row, cell.col) == (2, 1))
    assert tuple(ref.text for ref in value.headers) == ("Group", "Value")
    assert (
        f'cells.{value.cell_id} (2,1): 1,234 row=2 col=1 header="Group" | "Value"'
        in block.prompt_text()
    )
