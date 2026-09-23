"""A published diagram member is remounted only by repeating its proof from the source."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.diagram_publication import (
    parse_diagram_receipt,
    validate_diagram_member,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import index_draft, publish_draft, qualify_draft
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, ObjectDescription
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingScope
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)


def _published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalDocumentStore, ProcessingStore, ProcessingScope, RetrievalMember]:
    """Ingest, qualify, index and publish one authored diagram page, offline."""
    ingest, _ = ingest_generic_semantics(tmp_path, monkeypatch, diagram_page=True)
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
        embedder=OfflineDescriptionEmbedder(),
    )
    published = publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )
    sources = LocalDocumentStore(source_store)
    outputs = ProcessingStore(processing_store)
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.DIAGRAM)
    return sources, outputs, plan.scope, member


def test_published_diagram_replays_its_proof_from_the_pinned_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, scope, member = _published(tmp_path, monkeypatch)
    ir, description, qualification = validate_diagram_member(sources, outputs.assets, scope, member)
    assert tuple(node.label for node in ir.nodes) == ("PLAN", "BUILD")
    assert description.text == "Diagram with 2 nodes and 1 edge: PLAN; BUILD. PLAN -> BUILD."
    assert qualification.scope == "diagram-structure-source-geometry-v1"
    assert tuple(node.node_id for node in qualification.nodes) == ("n1", "n2")
    (edge,) = qualification.edges
    assert (edge.source_node_id, edge.target_node_id) == ("n1", "n2")
    assert edge.tip == (150.0, 85.0)
    # Replay is a pure function of the pinned bytes: twice is the same value.
    assert validate_diagram_member(sources, outputs.assets, scope, member) == (
        ir,
        description,
        qualification,
    )


def test_a_rewritten_projection_is_refused_even_with_a_matching_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, scope, member = _published(tmp_path, monkeypatch)
    receipt = parse_diagram_receipt(outputs.assets.get(member.qualification))
    stored = TypeAdapter(ObjectDescription).validate_json(outputs.assets.get(member.description))
    tampered = outputs.assets.put(
        TypeAdapter(ObjectDescription).dump_json(
            replace(stored, text=stored.text + " BUILD precedes PLAN.")
        ),
        media_type="application/json",
    )
    forged = outputs.assets.put(
        receipt.model_copy(update={"description": tampered}).model_dump_json().encode(),
        media_type="application/json",
    )
    rewritten = replace(member, description=tampered, qualification=forged)
    with pytest.raises(ValueError, match="differs from independent source qualification"):
        validate_diagram_member(sources, outputs.assets, scope, rewritten)


def test_an_incomplete_raw_branch_or_a_foreign_receipt_cannot_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, outputs, scope, member = _published(tmp_path, monkeypatch)
    assert len(member.lineage_refs) == 3
    with pytest.raises(ValueError, match="complete raw branch"):
        validate_diagram_member(
            sources, outputs.assets, scope, replace(member, lineage_refs=member.lineage_refs[:2])
        )
    receipt = parse_diagram_receipt(outputs.assets.get(member.qualification))
    foreign = outputs.assets.put(
        receipt.model_copy(update={"object_id": "another-object"}).model_dump_json().encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="does not match its retrieval member"):
        validate_diagram_member(
            sources, outputs.assets, scope, replace(member, qualification=foreign)
        )
    # The raw model branch must stay bound to the qualified anchor.
    raw = TypeAdapter(DiagramIR).validate_json(outputs.assets.get(receipt.raw_ir))
    unbound = outputs.assets.put(
        TypeAdapter(DiagramIR).dump_json(replace(raw, object_id="another-object")),
        media_type="application/json",
    )
    rebound = outputs.assets.put(
        receipt.model_copy(update={"raw_ir": unbound}).model_dump_json().encode(),
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="not bound to the qualified anchor"):
        validate_diagram_member(
            sources,
            outputs.assets,
            scope,
            replace(
                member,
                qualification=rebound,
                lineage_refs=(unbound, receipt.raw_description, receipt.view),
            ),
        )
