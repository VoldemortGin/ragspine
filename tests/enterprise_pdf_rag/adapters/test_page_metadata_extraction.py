"""The page metadata stage: one text-only call per page, verbatim values, cached, deferred without budget."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pytest import CaptureFixture

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.processing_review import processing_status
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.page_metadata_extraction import (
    annotate_page_metadata,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.processing.index_text import PageIndexContext
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import load_llm_config
from ragspine.extraction.evidence.metadata.document_metadata import DocumentMetadata
from ragspine.extraction.evidence.metadata.page_metadata import (
    MetadataValue,
    PageMetadata,
    PageType,
)
from ragspine.extraction.evidence.page.models import StageState
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import (
    ingest_with_metadata,
    publish_with_metadata,
)


def test_semantics_stage_annotates_every_page_with_verbatim_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls, metadata_calls = ingest_with_metadata(tmp_path, monkeypatch)

    assert ingest.stage == "semantics"
    assert len(calls) == 3 and len(metadata_calls) == 3  # layout + metadata, once per page
    assert ingest.live_call_count == 6
    assert ingest.metadata_page_states == {"succeeded": 3}
    assert ingest.metadata_status.startswith("attempted")
    assert ingest.display_title == "Meridian 1H26 Hong Kong page 1"
    assert ingest.failed_stage_count == 0

    outputs = ProcessingStore(Path(ingest.processing_store))
    manifest = outputs.load(ingest.processing_id)
    assert manifest.retrieval is None
    pages = outputs.load_page_metadata(manifest)
    assert sorted(pages) == [0, 1, 2]
    for page in manifest.pages:
        stage = page.metadata
        assert stage is not None and stage.state is StageState.SUCCEEDED
        assert stage.stage == "page_metadata" and stage.producer.startswith("page-metadata-v1.2:")
        assert stage.artifact is not None
        stored = TypeAdapter(PageMetadata).validate_json(outputs.assets.get(stage.artifact))
        assert stored == pages[page.page_index]
    cover = pages[0]
    assert cover.page_type is PageType.COVER and pages[1].page_type is PageType.TEXT
    assert cover.title is not None and cover.title.text == "Meridian 1H26 Hong Kong page 1"
    assert cover.title.evidence.text == "Meridian 1H26 Hong Kong page 1"
    assert cover.normalized_periods == ("1H2026",)
    assert cover.periods[0].text == "1H26"
    assert cover.periods[0].evidence.span_ids == cover.title.evidence.span_ids
    assert tuple(region.text for region in cover.regions) == ("Hong Kong",)
    assert any("'Mars'" in line and "not verbatim" in line for line in cover.diagnostics)

    document = manifest.document_metadata
    assert document is not None
    assert document.display_title == cover.title
    assert document.report_period is not None and document.report_period.normalized == "1H2026"
    assert document.years == (2026,) and document.language == "en"
    assert tuple(region.text for region in document.regions) == ("Hong Kong",)
    assert document.page_count == 3 and document.cover_page_index == 0
    assert outputs.index_contexts(manifest)[1] == PageIndexContext(
        "Meridian 1H26 Hong Kong page 1", "Meridian 1H26 Hong Kong page 2", None
    )
    status = processing_status(ingest.processing_id, manifest)
    assert status.deferred_stages == 0 and status.failed_stages == 0

    # The prompt carries the page's spans as data and asks for verbatim citations only.
    assert '"text":"Meridian 1H26 Hong Kong page 1"' in metadata_calls[0]
    assert "Physical page: 1." in metadata_calls[0]
    assert "verbatim" in metadata_calls[0]


def test_metadata_stage_alone_runs_over_the_source_stage_and_replays_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, calls, metadata_calls = ingest_with_metadata(
        tmp_path, monkeypatch, stage="metadata", max_live_calls=3
    )
    assert ingest.stage == "metadata"
    assert calls == [] and len(metadata_calls) == 3  # no layout call at all
    assert ingest.live_call_count == 3
    assert ingest.metadata_page_states == {"succeeded": 3}
    assert ingest.semantic_status.startswith("deferred")
    outputs = ProcessingStore(Path(ingest.processing_store))
    manifest = outputs.load(ingest.processing_id)
    assert all(page.partition.state is StageState.DEFERRED for page in manifest.pages)
    assert len(outputs.load_page_metadata(manifest)) == 3

    replay, _, replay_calls = ingest_with_metadata(
        tmp_path, monkeypatch, stage="metadata", max_live_calls=0
    )
    assert replay.processing_id == ingest.processing_id  # content-addressed, same draft
    assert replay.live_call_count == 0 and replay_calls == []
    assert replay.metadata_page_states == {"succeeded": 3}


def test_without_budget_the_stage_is_deferred_with_a_diagnostic_never_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _, metadata_calls = ingest_with_metadata(
        tmp_path, monkeypatch, stage="metadata", max_live_calls=1
    )
    assert len(metadata_calls) == 1
    assert ingest.metadata_page_states == {"deferred": 2, "succeeded": 1}
    assert ingest.failed_stage_count == 0
    outputs = ProcessingStore(Path(ingest.processing_store))
    manifest = outputs.load(ingest.processing_id)
    deferred = [page.metadata for page in manifest.pages[1:]]
    assert all(
        stage is not None
        and stage.state is StageState.DEFERRED
        and stage.diagnostic == "Page metadata model call did not complete: call_budget_exhausted"
        for stage in deferred
    )
    assert manifest.document_metadata is not None
    assert manifest.document_metadata.page_count == 1
    assert processing_status(ingest.processing_id, manifest).deferred_stages == 2

    # Without a model there is no producer identity to replay a cache under: all deferred.
    sources = LocalDocumentStore(Path(ingest.source_store), activate_on_publish=False)
    unconfigured = annotate_page_metadata(
        sources, outputs, processing_id=ingest.processing_id, client=None
    )
    assert unconfigured.page_states == {"deferred": 3}
    assert unconfigured.live_call_count == 0
    deferred_manifest = outputs.load(unconfigured.annotated_processing_id)
    assert deferred_manifest.document_metadata is None
    assert all(
        page.metadata is not None
        and page.metadata.diagnostic == "No answer model is configured; page metadata has not run."
        for page in deferred_manifest.pages
    )


def test_invalid_model_output_fails_the_page_stage_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _, _ = ingest_with_metadata(tmp_path, monkeypatch, stage="source", max_live_calls=0)
    monkeypatch.setattr(
        "ragspine.common.evidence.providers.json_completion._send_once",
        lambda url, *, api_key, payload, timeout: json.dumps(
            {
                "choices": [
                    {"message": {"content": '{"page_type":"poster"}'}, "finish_reason": "stop"}
                ]
            }
        ).encode(),
    )
    sources = LocalDocumentStore(Path(ingest.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(ingest.processing_store))
    client = JsonCompletionClient(
        load_llm_config(), cache_dir=outputs.root / "model-cache", max_live_calls=3
    )
    annotated = annotate_page_metadata(
        sources, outputs, processing_id=ingest.processing_id, client=client
    )
    assert annotated.page_states == {"failed": 3}
    assert annotated.display_title is None and annotated.years == ()
    manifest = outputs.load(annotated.annotated_processing_id)
    assert manifest.document_metadata is None
    assert all(
        page.metadata is not None
        and page.metadata.state is StageState.FAILED
        and page.metadata.diagnostic
        == "Page metadata model call did not complete: invalid_model_json"
        for page in manifest.pages
    )


def test_document_metadata_must_match_its_page_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _, _ = ingest_with_metadata(tmp_path, monkeypatch)
    sources = LocalDocumentStore(Path(ingest.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(ingest.processing_store))
    manifest = outputs.load(ingest.processing_id)
    assert manifest.document_metadata is not None
    title = manifest.document_metadata.display_title
    assert title is not None
    forged = replace(
        manifest,
        document_metadata=replace(
            manifest.document_metadata,
            display_title=MetadataValue("Forged title", title.evidence),
        ),
    )
    with pytest.raises(ValueError, match="Document metadata differs"):
        outputs.save_draft(forged, sources=sources)
    with pytest.raises(ValueError, match="Document metadata differs"):
        outputs.save_draft(replace(manifest, document_metadata=None), sources=sources)


def test_cli_metadata_command_annotates_a_saved_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    ingest, _, _ = ingest_with_metadata(tmp_path, monkeypatch, stage="source", max_live_calls=0)
    for stage in ("metadata", "replay"):
        assert (
            main(
                [
                    "metadata",
                    "--source-store",
                    ingest.source_store,
                    "--processing-store",
                    ingest.processing_store,
                    "--processing-id",
                    ingest.processing_id,
                    "--max-live-calls",
                    "3" if stage == "metadata" else "0",
                ]
            )
            == 0
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["processing_id"] == ingest.processing_id
        assert summary["annotated_processing_id"] != ingest.processing_id
        assert summary["page_states"] == {"succeeded": 3}
        assert summary["live_call_count"] == (3 if stage == "metadata" else 0)
        assert summary["display_title"] == "Meridian 1H26 Hong Kong page 1"
        assert summary["report_period"] == "1H2026"
        assert summary["years"] == [2026] and summary["regions"] == ["Hong Kong"]
        assert summary["indexed"] is False and summary["activated"] is False
        assert [page["page_type"] for page in summary["pages"]] == ["cover", "text", "text"]
        assert summary["pages"][0]["dropped"]
    assert (
        main(
            [
                "metadata",
                "--source-store",
                ingest.source_store,
                "--processing-store",
                ingest.processing_store,
                "--processing-id",
                "0" * 64,
                "--max-live-calls",
                "0",
            ]
        )
        == 1
    )
    assert "error" in json.loads(capsys.readouterr().out)


def test_catalog_entries_carry_the_document_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ingestion"
    meridian = publish_with_metadata(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian 1H26 Hong Kong",
        page_count=2,
        output_dir=root,
    )
    orion = publish_with_metadata(
        tmp_path,
        monkeypatch,
        filename="orion.pdf",
        label="Orion FY2024 Thailand",
        page_count=2,
        output_dir=root,
    )
    catalog = scan_catalog(root)
    assert len(catalog.ready) == 2
    first = catalog.entry(meridian.source_sha256)
    second = catalog.entry(orion.source_sha256)
    assert first is not None and second is not None
    assert first.display_title == "Meridian 1H26 Hong Kong page 1"
    assert first.display_name == first.display_title
    assert (first.report_period, first.years, first.regions, first.language) == (
        "1H2026",
        (2026,),
        ("Hong Kong",),
        "en",
    )
    assert second.display_title == "Orion FY2024 Thailand page 1"
    assert (second.report_period, second.years, second.regions) == (
        "FY2024",
        (2024,),
        ("Thailand",),
    )
    assert TypeAdapter(DocumentMetadata)  # the record round-trips through the manifest envelope
    manifest = ProcessingStore(Path(first.processing_store)).load(first.current_processing_id or "")
    assert manifest.document_metadata is not None and manifest.retrieval is not None


def test_index_text_carries_the_contextual_header_under_policy_v5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = _RecordingEmbedder()
    published = publish_with_metadata(
        tmp_path,
        monkeypatch,
        filename="meridian.pdf",
        label="Meridian 1H26 Hong Kong",
        page_count=2,
        output_dir=tmp_path / "ingestion",
        embedder=embedder,
    )
    header = "Meridian 1H26 Hong Kong page 1 | Meridian 1H26 Hong Kong page 2\n"
    assert any(text.startswith(header) for text in embedder.descriptions)
    # The cover page's own title is its header: display title and page title coincide.
    assert any(
        text.startswith("Meridian 1H26 Hong Kong page 1 | Meridian 1H26 Hong Kong page 1\n")
        for text in embedder.descriptions
    )
    entry = scan_catalog(tmp_path / "ingestion").entry(published.source_sha256)
    assert entry is not None
    mount = mount_document(entry, embedder=embedder)
    texts = mount.member_texts()
    assert [text.text for text in texts] == embedder.descriptions[: len(texts)] or {
        text.text for text in texts
    } == set(embedder.descriptions)
    second = next(text for text in texts if text.page_index == 1)
    assert second.page_title == "Meridian 1H26 Hong Kong page 2"
    assert second.page_type == "text" and second.section is None
    assert second.periods == ("1H2026",) and second.regions == ("Hong Kong",)
    assert next(text for text in texts if text.page_index == 0).page_type == "cover"
    plan, _ = ProcessingStore(Path(entry.processing_store)).load_retrieval(
        mount.manifest().retrieval  # type: ignore[arg-type]
    )
    assert plan.qualification_policy == "source-transcription-and-scoped-chart-qualification-v5"
    # Descriptions and quoted evidence are untouched by the header.
    block = mount.resolve(PinnedRetrievalHit(plan.snapshot_id, second.member_id, 1.0))
    assert block.description.text == "Meridian 1H26 Hong Kong page 2"


class _RecordingEmbedder(OfflineDescriptionEmbedder):
    def __init__(self) -> None:
        self.descriptions: list[str] = []

    def embed_description(self, text: str) -> tuple[float, ...]:
        self.descriptions.append(text)
        return super().embed_description(text)
