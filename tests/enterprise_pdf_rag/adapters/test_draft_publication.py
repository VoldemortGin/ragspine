"""Generic draft qualification diagnoses retrievable members without model calls."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import CaptureFixture

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftIndex,
    DraftPublication,
    DraftQualification,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.http.processing_schemas import ProcessingEnvelope
from enterprise_pdf_rag.adapters.object_processing import ProcessingObjectAdapter
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.documents.models import (
    DocumentManifest,
    PageRecord,
    RegionRecord,
    TextSidecar,
    TextSpan,
)
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
    PageProcessingRecord,
    ProcessingManifest,
    ProcessingScope,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.service import canonical_page
from enterprise_pdf_rag.processing.typed_ir import (
    LiteralQualification,
    ObjectDescription,
    TextIR,
)


def _publish_source(
    sources: LocalDocumentStore,
) -> tuple[str, str, PageInput, LayoutObject]:
    pdf = sources.put(b"unit-test source", media_type="application/pdf")
    svg = sources.put(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><path d="M0 0L10 10"/></svg>',
        media_type="image/svg+xml",
    )
    sidecar = TextSidecar(
        "source-text-v1",
        pdf.sha256,
        0,
        (TextSpan("number", "Agency 72%", (10.0, 10.0, 70.0, 20.0)),),
    )
    text = sources.put(json.dumps(asdict(sidecar)).encode(), media_type="application/json")
    source_id = sources.publish(
        DocumentManifest(
            "source-ingestion-v1",
            "test.pdf",
            pdf,
            "test",
            (PageRecord(0, 100.0, 100.0, 0, svg, text, 1, ()),),
            RegionRecord(0, (0.0, 0.0, 100.0, 40.0), svg, svg, text, ()),
        )
    )
    page = PageInput(source_id, pdf.sha256, 0, 100.0, 100.0, svg, sidecar)
    item = LayoutObject(
        "object",
        ObjectKind.TEXT,
        (0.0, 0.0, 100.0, 40.0),
        ("number",),
        "literal transcription only",
        Confidence(None, "test"),
    )
    return source_id, pdf.sha256, page, item


def _canonical(outputs: ProcessingStore, page: PageInput) -> StageOutcome:
    ref = outputs.assets.put(
        TypeAdapter(CanonicalPage).dump_json(canonical_page(page)),
        media_type="application/json",
    )
    return StageOutcome("canonical", "1" * 64, StageState.SUCCEEDED, "test", ref)


def _partition(
    outputs: ProcessingStore, source_id: str, sha: str, item: LayoutObject
) -> StageOutcome:
    partition = PagePartition("layout-v2", source_id, sha, 0, "test", (item,), ())
    ref = outputs.assets.put(
        TypeAdapter(PagePartition).dump_json(partition), media_type="application/json"
    )
    return StageOutcome("partition", "2" * 64, StageState.SUCCEEDED, "test", ref)


def _semantics_draft(tmp_path: Path) -> tuple[LocalDocumentStore, ProcessingStore, str]:
    sources = LocalDocumentStore(tmp_path / "source")
    outputs = ProcessingStore(tmp_path / "processed")
    source_id, sha, page, item = _publish_source(sources)
    record = ProcessingObjectAdapter(sources, outputs).process(page, item)
    scope = ProcessingScope(source_id, sha, 1, (0,))
    page_record = PageProcessingRecord(
        0,
        _canonical(outputs, page),
        _partition(outputs, source_id, sha, item),
        (record,),
    )
    manifest = ProcessingManifest("processing-v1", scope, "test", (page_record,))
    processing_id = outputs.save_draft(manifest, sources=sources)
    return sources, outputs, processing_id


def test_qualify_reports_eligible_members_and_kinds(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    result = qualify_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
    )
    assert isinstance(result, DraftQualification)
    assert result.processing_id == processing_id
    assert result.eligible_member_count == 1
    assert result.skipped_object_count == 0
    assert result.chart_member_count == 0
    assert result.kinds == {"Text": 1}
    assert result.skipped_reasons == {}
    assert result.indexed is False and result.activated is False
    assert result.retrieval_status == "qualified; indexing pending"


def test_qualify_source_only_draft_has_no_eligible_members(tmp_path: Path) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    outputs = ProcessingStore(tmp_path / "processed")
    source_id, sha, page, _item = _publish_source(sources)
    scope = ProcessingScope(source_id, sha, 1, (0,))
    page_record = PageProcessingRecord(
        0,
        _canonical(outputs, page),
        StageOutcome(
            "partition",
            "2" * 64,
            StageState.DEFERRED,
            "test",
            None,
            "layout deferred in the source stage",
        ),
        (),
    )
    manifest = ProcessingManifest("processing-v1", scope, "test", (page_record,))
    processing_id = outputs.save_draft(manifest, sources=sources)
    result = qualify_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
    )
    assert result.eligible_member_count == 0
    assert result.skipped_object_count == 0
    assert result.kinds == {}
    assert result.chart_member_count == 0
    assert result.source_manifest_id == source_id


def test_qualify_records_reason_for_objects_without_semantic_stages(
    tmp_path: Path,
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    outputs = ProcessingStore(tmp_path / "processed")
    source_id, sha, page, item = _publish_source(sources)
    scope = ProcessingScope(source_id, sha, 1, (0,))
    incomplete = ObjectProcessingRecord("object", ObjectKind.TEXT, ())
    page_record = PageProcessingRecord(
        0,
        _canonical(outputs, page),
        _partition(outputs, source_id, sha, item),
        (incomplete,),
    )
    manifest = ProcessingManifest("processing-v1", scope, "test", (page_record,))
    processing_id = outputs.save_draft(manifest, sources=sources)
    result = qualify_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
    )
    assert result.eligible_member_count == 0
    assert result.skipped_object_count == 1
    assert sum(result.skipped_reasons.values()) == 1


def test_qualify_rejects_tampered_source_evidence(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    manifest = outputs.load(processing_id)
    forged = replace(
        manifest,
        scope=replace(
            manifest.scope,
            source_page_count=manifest.scope.source_page_count + 1,
        ),
    )
    forged_ref = outputs.assets.put(
        ProcessingEnvelope(manifest=forged).model_dump_json().encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError):
        qualify_draft(
            source_store=sources.root,
            processing_store=outputs.root,
            processing_id=forged_ref.sha256,
        )


def test_cli_qualify_emits_expected_json(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    code = main(
        [
            "qualify",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            processing_id,
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["processing_id"] == processing_id
    assert payload["eligible_member_count"] == 1
    assert payload["retrieval_status"] == "qualified; indexing pending"
    assert payload["indexed"] is False and payload["activated"] is False


def test_cli_qualify_fails_closed_on_unknown_draft(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    sources = LocalDocumentStore(tmp_path / "source")
    outputs = ProcessingStore(tmp_path / "processed")
    code = main(
        [
            "qualify",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            "not-a-real-processing-id",
        ]
    )
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("error")


class RecordingEmbedding:
    """Records only the description text it is asked to embed; never SVG or IR."""

    fingerprint = "test-explicit-offline-vector-v1"

    def __init__(self) -> None:
        self.descriptions: list[str] = []

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.descriptions.append(text)
        return (1.0, 0.0)

    def embed_query(self, text: str) -> tuple[float, ...]:
        assert text.strip()
        return (1.0, 0.0)


def test_index_creates_new_snapshot_without_moving_pointer(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    embedder = RecordingEmbedding()
    result = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=embedder,
    )
    assert isinstance(result, DraftIndex)
    assert result.processing_id == processing_id
    assert result.indexed_processing_id != processing_id
    assert result.member_count == 1
    assert result.embedding_dimensions == (2,)
    assert result.embedding_fingerprint == embedder.fingerprint
    assert result.indexed is True and result.activated is False
    assert result.retrieval_status == "indexed; publication pending"
    assert embedder.descriptions == ["Agency 72%"]  # Never SVG or serialized IR.
    assert not (outputs.root / "current-processing").exists()
    assert outputs.load(processing_id).retrieval is None
    reindexed = outputs.load(result.indexed_processing_id)
    assert reindexed.retrieval is not None
    assert reindexed.retrieval.snapshot_id == result.retrieval_snapshot_id


def test_index_is_idempotent_for_identical_drafts(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    embedder = RecordingEmbedding()
    first = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=embedder,
    )
    second = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=embedder,
    )
    assert first.indexed_processing_id == second.indexed_processing_id
    assert first.retrieval_snapshot_id == second.retrieval_snapshot_id


def test_index_uses_document_label_for_review_title(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    result = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=RecordingEmbedding(),
        document_label="Quarterly Filing",
    )
    review = Path(result.review_path)
    assert review.is_file()
    assert "Quarterly Filing" in review.read_text(encoding="utf-8")


def test_index_fails_closed_on_tampered_evidence(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    manifest = outputs.load(processing_id)
    page_record = manifest.pages[0]
    record = page_record.objects[0]
    stages = {stage.stage: stage for stage in record.stages}
    description_source = stages["description"].artifact
    qualification_source = stages["qualification"].artifact
    ir_source = stages["ir"].artifact
    assert (
        description_source is not None
        and qualification_source is not None
        and ir_source is not None
    )
    description = TypeAdapter(ObjectDescription).validate_json(
        outputs.assets.get(description_source)
    )
    receipt = TypeAdapter(LiteralQualification).validate_json(
        outputs.assets.get(qualification_source)
    )
    ir = TypeAdapter(TextIR).validate_json(outputs.assets.get(ir_source))
    description_ref = outputs.assets.put(
        TypeAdapter(ObjectDescription).dump_json(replace(description, producer="forged-producer")),
        media_type="application/json",
    )
    ir_ref = outputs.assets.put(TypeAdapter(TextIR).dump_json(ir), media_type="application/json")
    receipt_ref = outputs.assets.put(
        TypeAdapter(LiteralQualification).dump_json(
            replace(receipt, description=description_ref, ir=ir_ref)
        ),
        media_type="application/json",
    )
    for name, ref in (
        ("description", description_ref),
        ("ir", ir_ref),
        ("qualification", receipt_ref),
    ):
        stages[name] = replace(stages[name], artifact=ref)
    forged_record = replace(record, stages=tuple(stages.values()))
    forged = replace(manifest, pages=(replace(page_record, objects=(forged_record,)),))
    forged_ref = outputs.assets.put(
        ProcessingEnvelope(manifest=forged).model_dump_json().encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="producer"):
        index_draft(
            source_store=sources.root,
            processing_store=outputs.root,
            processing_id=forged_ref.sha256,
            embedder=RecordingEmbedding(),
        )


def test_cli_index_emits_expected_json(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("EMBEDDING_MODEL", "offline-test-model")
    monkeypatch.setenv("EMBEDDING_API_KEY", "offline-test-key")
    monkeypatch.setattr(
        "enterprise_pdf_rag.cli.LocalEmbeddingAdapter",
        lambda config: OfflineDescriptionEmbedder(),
    )
    code = main(
        [
            "index",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            processing_id,
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["processing_id"] == processing_id
    assert payload["indexed_processing_id"] != processing_id
    assert payload["member_count"] == 1
    assert payload["embedding_dimensions"] == [64]
    assert payload["indexed"] is True and payload["activated"] is False
    assert payload["retrieval_status"] == "indexed; publication pending"
    assert not (outputs.root / "current-processing").exists()


def test_cli_index_fails_closed_on_missing_embedding_config(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    for name in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    code = main(
        [
            "index",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            processing_id,
        ]
    )
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("error")


def _indexed_draft(tmp_path: Path) -> tuple[LocalDocumentStore, ProcessingStore, str]:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    indexed = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=RecordingEmbedding(),
    )
    return sources, outputs, indexed.indexed_processing_id


def test_publish_moves_processing_pointer_and_activates_source(tmp_path: Path) -> None:
    sources, outputs, indexed_id = _indexed_draft(tmp_path)
    (sources.root / "current-manifest").unlink()
    assert not (outputs.root / "current-processing").exists()
    result = publish_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=indexed_id,
    )
    assert isinstance(result, DraftPublication)
    assert result.processing_id == indexed_id
    assert result.published_processing_id == indexed_id
    assert result.current_processing_id == indexed_id
    assert result.member_count == 1
    assert result.embedding_dimensions == (2,)
    assert result.indexed is True and result.activated is True
    assert result.source_activated is True
    assert result.retrieval_status == "ready"
    current_id, current_manifest = outputs.load_current()
    assert current_id == indexed_id
    assert current_manifest.retrieval is not None
    assert sources.load_current().manifest_id == result.source_manifest_id


def test_publish_without_source_activation_leaves_current_manifest(
    tmp_path: Path,
) -> None:
    sources, outputs, indexed_id = _indexed_draft(tmp_path)
    (sources.root / "current-manifest").unlink()
    result = publish_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=indexed_id,
        activate_source=False,
    )
    assert result.source_activated is False
    assert result.activated is True
    assert not (sources.root / "current-manifest").exists()
    assert (outputs.root / "current-processing").read_text().strip() == indexed_id


def test_publish_requires_a_retrieval_index(tmp_path: Path) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    with pytest.raises(ValueError, match="index"):
        publish_draft(
            source_store=sources.root,
            processing_store=outputs.root,
            processing_id=processing_id,
        )
    assert not (outputs.root / "current-processing").exists()


def test_publish_is_idempotent(tmp_path: Path) -> None:
    sources, outputs, indexed_id = _indexed_draft(tmp_path)
    first = publish_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=indexed_id,
    )
    second = publish_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=indexed_id,
    )
    assert first.published_processing_id == second.published_processing_id == indexed_id
    assert first.current_processing_id == second.current_processing_id == indexed_id
    assert (outputs.root / "current-processing").read_text().strip() == indexed_id
    assert sources.load_current().manifest_id == first.source_manifest_id


def test_cli_publish_emits_expected_json(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    sources, outputs, indexed_id = _indexed_draft(tmp_path)
    code = main(
        [
            "publish",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            indexed_id,
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published_processing_id"] == indexed_id
    assert payload["current_processing_id"] == indexed_id
    assert payload["retrieval_status"] == "ready"
    assert payload["indexed"] is True and payload["activated"] is True
    assert payload["source_activated"] is True
    assert (outputs.root / "current-processing").read_text().strip() == indexed_id


def test_cli_publish_no_activate_source(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    sources, outputs, indexed_id = _indexed_draft(tmp_path)
    (sources.root / "current-manifest").unlink()
    code = main(
        [
            "publish",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            indexed_id,
            "--no-activate-source",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_activated"] is False
    assert not (sources.root / "current-manifest").exists()


def test_cli_publish_fails_closed_on_unindexed_draft(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    sources, outputs, processing_id = _semantics_draft(tmp_path)
    code = main(
        [
            "publish",
            "--source-store",
            str(sources.root),
            "--processing-store",
            str(outputs.root),
            "--processing-id",
            processing_id,
        ]
    )
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("error")


def _source_only_draft(
    tmp_path: Path,
) -> tuple[LocalDocumentStore, ProcessingStore, str]:
    sources = LocalDocumentStore(tmp_path / "source")
    outputs = ProcessingStore(tmp_path / "processed")
    source_id, sha, page, _item = _publish_source(sources)
    scope = ProcessingScope(source_id, sha, 1, (0,))
    page_record = PageProcessingRecord(
        0,
        _canonical(outputs, page),
        StageOutcome(
            "partition",
            "2" * 64,
            StageState.DEFERRED,
            "test",
            None,
            "layout deferred in the source stage",
        ),
        (),
    )
    manifest = ProcessingManifest("processing-v1", scope, "test", (page_record,))
    processing_id = outputs.save_draft(manifest, sources=sources)
    return sources, outputs, processing_id


def test_relative_stores_resolve_to_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sources, _outputs, processing_id = _semantics_draft(tmp_path)
    monkeypatch.chdir(tmp_path)
    indexed = index_draft(
        source_store=Path("source"),
        processing_store=Path("processed"),
        processing_id=processing_id,
        embedder=RecordingEmbedding(),
    )
    review = Path(indexed.review_path)
    assert review.is_absolute()
    assert review == (
        (tmp_path / "processed").resolve() / "runs" / indexed.indexed_processing_id / "review.html"
    )
    published = publish_draft(
        source_store=Path("source"),
        processing_store=Path("processed"),
        processing_id=indexed.indexed_processing_id,
    )
    assert Path(published.source_store).is_absolute()
    assert Path(published.processing_store).is_absolute()
    assert published.source_store == str((tmp_path / "source").resolve())
    assert published.processing_store == str((tmp_path / "processed").resolve())


def test_source_only_draft_produces_publishable_empty_index(tmp_path: Path) -> None:
    sources, outputs, processing_id = _source_only_draft(tmp_path)
    embedder = RecordingEmbedding()
    indexed = index_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=processing_id,
        embedder=embedder,
    )
    assert indexed.member_count == 0
    assert indexed.embedding_dimensions == ()
    assert embedder.descriptions == []
    assert indexed.indexed_processing_id != processing_id
    assert not (outputs.root / "current-processing").exists()
    published = publish_draft(
        source_store=sources.root,
        processing_store=outputs.root,
        processing_id=indexed.indexed_processing_id,
    )
    assert published.retrieval_status == "ready"
    assert published.member_count == 0
    assert published.current_processing_id == indexed.indexed_processing_id
    assert (
        outputs.root / "current-processing"
    ).read_text().strip() == indexed.indexed_processing_id
