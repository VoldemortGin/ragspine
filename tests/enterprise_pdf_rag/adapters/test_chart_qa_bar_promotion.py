"""Adding a bar requires its own description vector; existing vectors are immutable."""

from dataclasses import replace
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_qa_bar_promotion import append_displayed_member
from enterprise_pdf_rag.processing.retrieval import RetrievalEmbedding
from ragspine.extraction.evidence.document.models import AssetRef
from tests.enterprise_pdf_rag.adapters.test_chart_qa_store import published_chart


def test_new_bar_member_adds_one_bound_vector_without_reembedding_the_old_members(
    tmp_path: Path,
) -> None:
    _, outputs, pin = published_chart(tmp_path, labels=True)
    prior = outputs.load(pin.processing_id)
    assert prior.retrieval is not None
    plan, index = outputs.load_retrieval(prior.retrieval)
    old = plan.members[0]
    member = replace(
        old,
        object_id="another-chart",
        description=AssetRef("e" * 64, "application/json", 19),
        embedding=AssetRef("f" * 64, "application/json", 23),
    )
    embedding = RetrievalEmbedding(
        member.description.sha256,
        old.embedding_fingerprint,
        tuple(float(i) for i in range(old.embedding_dimensions)),
    )
    result, result_index = append_displayed_member(plan, index, member, embedding)
    assert result.members == (*plan.members, member)
    assert result.snapshot_id != plan.snapshot_id
    assert result_index.snapshot_id == result.snapshot_id
    entries = {entry.member_id: entry.vector for entry in result_index.entries}
    assert len(entries) == len(index.entries) + 1
    assert all(entries[item.member_id] == item.vector for item in index.entries)
    assert entries[member.member_id] == embedding.vector
    assert plan.members == (old,)
    assert outputs.load_current()[0] == pin.processing_id


@pytest.mark.parametrize(
    "fault",
    (
        "description",
        "model",
        "dimensions",
        "duplicate_object",
        "cross_snapshot",
        "missing_old_vector",
    ),
)
def test_new_member_cannot_reuse_a_mismatched_vector_or_mix_embedding_models(
    tmp_path: Path, fault: str
) -> None:
    _, outputs, pin = published_chart(tmp_path, labels=True)
    prior = outputs.load(pin.processing_id)
    assert prior.retrieval is not None
    plan, index = outputs.load_retrieval(prior.retrieval)
    old = plan.members[0]
    member = replace(
        old,
        object_id="another-chart",
        description=AssetRef("e" * 64, "application/json", 19),
        embedding=AssetRef("f" * 64, "application/json", 23),
    )
    embedding = RetrievalEmbedding(
        member.description.sha256,
        old.embedding_fingerprint,
        tuple(float(i) for i in range(old.embedding_dimensions)),
    )
    if fault == "description":
        embedding = replace(embedding, description_sha256=old.description.sha256)
    elif fault == "model":
        member = replace(member, embedding_fingerprint="another-model")
        embedding = replace(embedding, fingerprint="another-model")
    elif fault == "dimensions":
        member = replace(member, embedding_dimensions=old.embedding_dimensions + 1)
        embedding = replace(embedding, vector=(*embedding.vector, 0.1))
    elif fault == "duplicate_object":
        member = replace(member, object_id=old.object_id)
    elif fault == "cross_snapshot":
        index = replace(index, snapshot_id="f" * 64)
    else:
        index = replace(index, entries=())
    with pytest.raises(ValueError):
        append_displayed_member(plan, index, member, embedding)
