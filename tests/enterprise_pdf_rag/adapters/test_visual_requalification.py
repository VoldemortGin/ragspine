"""Re-proving a snapshot's Diagram objects reads stored bytes only and moves no pointer."""

from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.aia_ingestion import AIA_OUTPUT
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_retrieval import eligibility
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.visual_requalification import (
    ObjectRequalification,
    requalify_visual_objects,
)
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingManifest,
    StageState,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)

# The legacy AIA release; only this module's AIA test names the location.
_AIA_SOURCES = AIA_OUTPUT
_AIA_PROCESSING = AIA_OUTPUT / "pages-001-020"
_PROVEN_STAGES = ("qualified_ir", "qualified_description", "qualification")


def _diagram(manifest: ProcessingManifest) -> ObjectProcessingRecord:
    (record,) = tuple(
        item for page in manifest.pages for item in page.objects if item.kind is ObjectKind.DIAGRAM
    )
    return record


def _stages(record: ObjectProcessingRecord) -> dict[str, tuple[StageState, object]]:
    return {stage.stage: (stage.state, stage.artifact) for stage in record.stages}


def _ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, diagram_caption: bool = False
) -> tuple[LocalDocumentStore, ProcessingStore, str]:
    ingest, _ = ingest_generic_semantics(
        tmp_path, monkeypatch, diagram_page=True, diagram_caption=diagram_caption
    )
    assert ingest.failed_stage_count == 0
    return (
        LocalDocumentStore(Path(ingest.source_store), activate_on_publish=False),
        ProcessingStore(Path(ingest.processing_store)),
        ingest.processing_id,
    )


def test_stripped_diagram_proof_is_rebuilt_byte_for_byte_from_the_stored_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot lacking the proof gains exactly the artifacts the ingest-time proof wrote."""
    sources, outputs, processing_id = _ingested(tmp_path, monkeypatch)
    manifest = outputs.load(processing_id)
    proven = _stages(_diagram(manifest))
    assert all(proven[name][0] is StageState.SUCCEEDED for name in _PROVEN_STAGES)

    unproven = replace(
        manifest,
        pages=tuple(
            replace(
                page,
                objects=tuple(
                    replace(
                        record,
                        stages=tuple(
                            stage for stage in record.stages if stage.stage not in _PROVEN_STAGES
                        ),
                        qualified_claim_count=0,
                    )
                    if record.kind is ObjectKind.DIAGRAM
                    else record
                    for record in page.objects
                ),
            )
            for page in manifest.pages
        ),
        producer="diagram-proof-removed-for-test",
    )
    unproven_id = outputs.save_draft(unproven, sources=sources)
    assert eligibility(_diagram(outputs.load(unproven_id))) == (
        False,
        "Diagram structure is not proven; only geometry-qualified diagrams are retrievable",
    )

    dry = requalify_visual_objects(sources, outputs, processing_id=unproven_id, dry_run=True)
    assert dry.draft_processing_id is None
    assert dry.objects == (
        ObjectRequalification(
            2, _diagram(manifest).object_id, ObjectKind.DIAGRAM, "qualified", None, 3
        ),
    )
    assert _stages(_diagram(outputs.load(unproven_id))) == _stages(_diagram(unproven))

    summary = requalify_visual_objects(sources, outputs, processing_id=unproven_id)
    assert summary.objects == dry.objects
    assert summary.draft_processing_id is not None
    revised = outputs.load(summary.draft_processing_id)
    record = _diagram(revised)
    assert record.qualified_claim_count == 3
    assert eligibility(record) == (True, None)
    # The proof is a pure function of the stored branches: the same three artifacts.
    assert {name: _stages(record)[name] for name in _PROVEN_STAGES} == {
        name: proven[name] for name in _PROVEN_STAGES
    }
    # Re-proving the already proven draft is a no-op, so no further draft is saved.
    assert requalify_visual_objects(
        sources, outputs, processing_id=summary.draft_processing_id
    ).objects == (
        ObjectRequalification(2, record.object_id, ObjectKind.DIAGRAM, "unchanged", None, 3),
    )


def test_an_unproven_diagram_is_withheld_with_its_verbatim_geometry_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, processing_id = _ingested(tmp_path, monkeypatch, diagram_caption=True)
    summary = requalify_visual_objects(sources, outputs, processing_id=processing_id, dry_run=True)

    (verdict,) = summary.objects
    assert verdict.outcome == "withheld" and verdict.qualified_claim_count == 0
    assert (verdict.diagnostic or "").startswith("object: uncited_source_span:")

    written = requalify_visual_objects(sources, outputs, processing_id=processing_id)
    assert written.draft_processing_id is not None
    record = _diagram(outputs.load(written.draft_processing_id))
    stages = {stage.stage: stage for stage in record.stages}
    assert "qualified_ir" not in stages and "qualified_description" not in stages
    assert stages["qualification"].state is StageState.UNAVAILABLE
    assert stages["qualification"].diagnostic == verdict.diagnostic
    assert eligibility(record) == (
        False,
        "Diagram structure is not proven; only geometry-qualified diagrams are retrievable",
    )


@pytest.mark.skipif(
    not (_AIA_PROCESSING / "current-processing").is_file(), reason="AIA sample store absent"
)
def test_aia_release_dry_run_proves_one_diagram_and_withholds_the_other() -> None:
    """Read-only over the real release: the verdicts, and not one byte of store state."""
    before = (
        (_AIA_SOURCES / "current-manifest").read_bytes(),
        (_AIA_PROCESSING / "current-processing").read_bytes(),
        sum(1 for _ in (_AIA_SOURCES / "objects" / "sha256").iterdir()),
        sum(1 for _ in (_AIA_PROCESSING / "objects" / "sha256").iterdir()),
    )
    sources = LocalDocumentStore(_AIA_SOURCES, activate_on_publish=False)
    outputs = ProcessingStore(_AIA_PROCESSING)
    processing_id, _ = outputs.load_current()

    summary = requalify_visual_objects(sources, outputs, processing_id=processing_id, dry_run=True)

    assert summary.processing_id == processing_id
    assert summary.draft_processing_id is None
    verdicts = {verdict.page_index: verdict for verdict in summary.objects}
    assert sorted(verdicts) == [4, 5]
    # Physical page 5: five nodes, one of which labels nothing the page actually prints.
    assert verdicts[4].object_id == "aia-p005-technology-flow-v1"
    assert verdicts[4].outcome == "withheld"
    assert (
        verdicts[4].diagnostic
        == "node node-industry-leading-technology: empty_label_without_source_occurrence"
    )
    assert verdicts[4].qualified_claim_count == 0
    # Physical page 6: three native frames, each labelled verbatim, joined by no edge.
    assert verdicts[5].outcome == "qualified"
    assert verdicts[5].diagnostic is None
    assert verdicts[5].qualified_claim_count == 3

    assert (
        (_AIA_SOURCES / "current-manifest").read_bytes(),
        (_AIA_PROCESSING / "current-processing").read_bytes(),
        sum(1 for _ in (_AIA_SOURCES / "objects" / "sha256").iterdir()),
        sum(1 for _ in (_AIA_PROCESSING / "objects" / "sha256").iterdir()),
    ) == before
