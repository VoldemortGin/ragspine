"""Numerical admission creates a new release and reuses only identical embeddings."""

from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver
from enterprise_pdf_rag.adapters.chart_qa_promotion import (
    create_numeric_draft,
    replace_index_member,
)
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.chart_qa.models import QueryStatus, RefusalReason
from enterprise_pdf_rag.figures.chart_qa.service import ChartQAService
from tests.enterprise_pdf_rag.adapters.test_chart_qa_store import published_chart, stored_question


def test_source_numeric_draft_preserves_old_refusal_vectors_and_current(
    tmp_path: Path,
) -> None:
    sources, outputs, pin = published_chart(tmp_path, labels=True, actual_pdf=True)
    previous = outputs.load(pin.processing_id)
    assert previous.retrieval is not None
    prior_plan, prior_index = outputs.load_retrieval(previous.retrieval)
    old_service = ChartQAService(
        StoredChartResolver(sources, outputs, processing_id=pin.processing_id)
    )
    old_answer = old_service.answer(stored_question(pin))
    assert old_answer.refusal_reason is RefusalReason.UNQUALIFIED_MEMBER

    release = create_numeric_draft(
        sources, outputs, processing_id=pin.processing_id, member_id=pin.member_id
    )

    assert release.previous == pin
    assert release.current.processing_id != pin.processing_id
    assert release.current.snapshot_id != pin.snapshot_id
    assert release.current.member_id != pin.member_id
    assert release.reused_vectors == 1 and release.numeric_claim_count == 2
    assert outputs.load_current()[0] == pin.processing_id
    draft = outputs.load(release.current.processing_id)
    assert draft.retrieval is not None
    plan, index = outputs.load_retrieval(draft.retrieval)
    assert plan.members[0].description == prior_plan.members[0].description
    assert plan.members[0].embedding == prior_plan.members[0].embedding
    assert index.entries[0].vector == prior_index.entries[0].vector
    assert len(plan.members[0].lineage_refs) == 4
    assert draft.pages[0].objects[0].qualified_claim_count == 2
    assert release.source_paint_proof in plan.members[0].lineage_refs
    answer = ChartQAService(
        StoredChartResolver(sources, outputs, processing_id=release.current.processing_id)
    ).answer(stored_question(release.current))
    assert answer.status is QueryStatus.ANSWERED
    assert answer.answer is not None and str(answer.answer.value) == "72"
    assert old_service.answer(stored_question(pin)) == old_answer
    assert (
        create_numeric_draft(
            sources, outputs, processing_id=pin.processing_id, member_id=pin.member_id
        )
        == release
    )


def test_new_member_rebinds_index_without_modifying_vector_or_prior_plan(
    tmp_path: Path,
) -> None:
    _, outputs, pin = published_chart(tmp_path, labels=True)
    manifest = outputs.load(pin.processing_id)
    assert manifest.retrieval is not None
    plan, index = outputs.load_retrieval(manifest.retrieval)
    old = plan.members[0]
    new = replace(old, ir=AssetRef("f" * 64, "application/json", 10))
    next_plan, next_index = replace_index_member(plan, index, old.member_id, new)
    assert next_plan.snapshot_id != plan.snapshot_id
    assert next_plan.members == (new,)
    assert next_index.snapshot_id == next_plan.snapshot_id
    assert next_index.entries[0].member_id == new.member_id
    assert next_index.entries[0].vector == index.entries[0].vector
    assert plan.members == (old,)
    assert new.description == old.description and new.embedding == old.embedding
    assert replace_index_member(plan, index, old.member_id, new) == (
        next_plan,
        next_index,
    )


@pytest.mark.parametrize(
    "change",
    (
        "description",
        "embedding",
        "source_svg",
        "page_index",
        "object_id",
        "embedding_fingerprint",
        "embedding_dimensions",
    ),
)
def test_promotion_cannot_reuse_vector_for_different_description_or_lineage(
    tmp_path: Path, change: str
) -> None:
    _, outputs, pin = published_chart(tmp_path, labels=True)
    manifest = outputs.load(pin.processing_id)
    assert manifest.retrieval is not None
    plan, index = outputs.load_retrieval(manifest.retrieval)
    old = plan.members[0]
    new = replace(
        old,
        description=AssetRef("f" * 64, "application/json", 10)
        if change == "description"
        else old.description,
        embedding=AssetRef("f" * 64, "application/json", 10)
        if change == "embedding"
        else old.embedding,
        source_svg=AssetRef("f" * 64, "image/svg+xml", 10)
        if change == "source_svg"
        else old.source_svg,
        page_index=1 if change == "page_index" else old.page_index,
        object_id="another-object" if change == "object_id" else old.object_id,
        embedding_fingerprint="another-model"
        if change == "embedding_fingerprint"
        else old.embedding_fingerprint,
        embedding_dimensions=3 if change == "embedding_dimensions" else old.embedding_dimensions,
    )
    with pytest.raises(ValueError, match="unchanged"):
        replace_index_member(plan, index, old.member_id, new)


def test_validated_draft_preserves_current_pointer_and_missing_source_fails(
    tmp_path: Path,
) -> None:
    sources, outputs, pin = published_chart(tmp_path)
    manifest = replace(outputs.load(pin.processing_id), producer="new-immutable-draft")
    draft_id = outputs.save_draft(manifest, sources=sources)
    assert draft_id != pin.processing_id
    assert outputs.load(draft_id) == manifest
    assert outputs.load_current()[0] == pin.processing_id
    assert outputs.save_draft(manifest, sources=sources) == draft_id
    source = sources.load(manifest.scope.source_manifest_id)
    sources.asset_path(source.manifest.source).unlink()
    with pytest.raises(FileNotFoundError):
        outputs.save_draft(replace(manifest, producer="no-source"), sources=sources)
    assert (outputs.root / "current-processing").read_text().strip() == pin.processing_id


def test_draft_review_does_not_replace_the_active_review(tmp_path: Path) -> None:
    sources, outputs, pin = published_chart(tmp_path)
    current = export_processing_review(sources, outputs, pin.processing_id)
    previous = current.read_bytes()
    manifest = replace(outputs.load(pin.processing_id), producer="new-review-draft")
    draft_id = outputs.save_draft(manifest, sources=sources)
    review = export_processing_review(sources, outputs, draft_id, update_current=False)
    assert review == outputs.root / "runs" / draft_id / "review.html"
    assert review.is_file()
    assert current.read_bytes() == previous
    assert outputs.load_current()[0] == pin.processing_id
