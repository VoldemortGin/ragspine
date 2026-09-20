"""A source file identity alone is insufficient to identify a retrieval release."""

from dataclasses import replace

import pytest

from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingScope
from enterprise_pdf_rag.processing.retrieval import (
    PinnedRetrievalHit,
    RetrievalMember,
    RetrievalPlan,
    resolve_member,
)


def _member() -> RetrievalMember:
    ref = AssetRef("a" * 64, "application/json", 12)
    return RetrievalMember(
        "object-1",
        ObjectKind.CHART,
        17,
        ref,
        ref,
        ref,
        ref,
        AssetRef("b" * 64, "image/svg+xml", 20),
        "embedding-model-v1",
        2560,
    )


def test_description_qualification_and_embedding_changes_create_distinct_snapshots() -> (
    None
):
    scope = ProcessingScope("c" * 64, "d" * 64, 71, tuple(range(20)))
    member = _member()
    original = RetrievalPlan(scope, (member,), "field-policy-v1", "cosine-index-v1")
    different = AssetRef("e" * 64, "application/json", 21)
    variants = (
        replace(original, members=(replace(member, description=different),)),
        replace(original, members=(replace(member, qualification=different),)),
        replace(original, members=(replace(member, embedding_dimensions=512),)),
        replace(original, qualification_policy="field-policy-v2"),
    )
    assert (
        len({original.snapshot_id, *(variant.snapshot_id for variant in variants)}) == 5
    )
    hit = PinnedRetrievalHit(original.snapshot_id, member.member_id, 0.9)
    assert resolve_member(original, hit) == member
    with pytest.raises(ValueError, match="snapshot"):
        resolve_member(variants[0], hit)


def test_member_order_does_not_change_snapshot_and_unpublished_member_is_rejected() -> (
    None
):
    scope = ProcessingScope("c" * 64, "d" * 64, 71, tuple(range(20)))
    first = _member()
    second = replace(first, object_id="object-2", page_index=1)
    plan = RetrievalPlan(scope, (first, second), "policy-v1", "index-v1")
    assert plan.snapshot_id == replace(plan, members=(second, first)).snapshot_id
    with pytest.raises(ValueError, match="member"):
        resolve_member(plan, PinnedRetrievalHit(plan.snapshot_id, "absent", 0.9))
    with pytest.raises(ValueError, match="selected"):
        replace(plan, members=(replace(first, page_index=24),))
