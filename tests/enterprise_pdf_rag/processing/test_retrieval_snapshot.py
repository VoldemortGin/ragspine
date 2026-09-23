"""A source file identity alone is insufficient to identify a retrieval release."""

from dataclasses import replace

import pytest

from enterprise_pdf_rag.adapters.processing_retrieval import (
    _POLICY,
    CONTEXTUAL_POLICIES,
    PROJECTED_CHART_POLICIES,
    VISUAL_PROJECTION_POLICIES,
)
from enterprise_pdf_rag.processing.retrieval import (
    PinnedRetrievalHit,
    RetrievalMember,
    RetrievalPlan,
    resolve_member,
)
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingScope

_V4_POLICY = "source-transcription-and-scoped-chart-qualification-v4"


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


def test_description_qualification_and_embedding_changes_create_distinct_snapshots() -> None:
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
    assert len({original.snapshot_id, *(variant.snapshot_id for variant in variants)}) == 5
    hit = PinnedRetrievalHit(original.snapshot_id, member.member_id, 0.9)
    assert resolve_member(original, hit) == member
    with pytest.raises(ValueError, match="snapshot"):
        resolve_member(variants[0], hit)


def test_a_policy_upgrade_leaves_every_older_snapshot_id_untouched() -> None:
    # Frozen: a snapshot published under ADR 0013's v4 policy keeps this id forever, so it
    # stays mountable after ADR 0015 raised the live policy to v5.
    scope = ProcessingScope("c" * 64, "d" * 64, 71, tuple(range(20)))
    plan = RetrievalPlan(scope, (_member(),), _V4_POLICY, "immutable-cosine-index-v1")
    assert plan.snapshot_id == "2dce209f1e09bc79be7bfd3e66f9a83a159abaff38682126adc5e21bdf68d139"
    upgraded = replace(plan, qualification_policy=_POLICY)
    assert (
        upgraded.snapshot_id == "58c280f8f4adcb9bef97ea00951ccfe498fe99b89a87aeef5e351e5b6595ce9a"
    )
    assert _V4_POLICY in PROJECTED_CHART_POLICIES and _V4_POLICY in CONTEXTUAL_POLICIES
    assert _V4_POLICY not in VISUAL_PROJECTION_POLICIES


def test_member_order_does_not_change_snapshot_and_unpublished_member_is_rejected() -> None:
    scope = ProcessingScope("c" * 64, "d" * 64, 71, tuple(range(20)))
    first = _member()
    second = replace(first, object_id="object-2", page_index=1)
    plan = RetrievalPlan(scope, (first, second), "policy-v1", "index-v1")
    assert plan.snapshot_id == replace(plan, members=(second, first)).snapshot_id
    with pytest.raises(ValueError, match="member"):
        resolve_member(plan, PinnedRetrievalHit(plan.snapshot_id, "absent", 0.9))
    with pytest.raises(ValueError, match="selected"):
        replace(plan, members=(replace(first, page_index=24),))
