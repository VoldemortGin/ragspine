"""Omitted model aliases become explicit unassigned observations, never financial facts."""

from dataclasses import replace

import pytest

from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)
from enterprise_pdf_rag.processing.service import (
    preserve_omitted_occurrences,
    validate_partition,
)


@pytest.mark.parametrize("omitted", (1, 2))
def test_missing_model_occurrences_are_retained_with_individual_diagnostics(
    omitted: int,
) -> None:
    spans = tuple(
        TextSpan(f"s{n}", f"Value {n}", (float(n), 0.0, float(n) + 0.5, 1.0))
        for n in range(omitted + 1)
    )
    page = PageInput(
        "a" * 64,
        "b" * 64,
        0,
        100.0,
        100.0,
        AssetRef("c" * 64, "image/svg+xml", 1),
        TextSidecar("source-text-v1", "b" * 64, 0, spans),
    )
    item = LayoutObject(
        "object",
        ObjectKind.GROUP,
        (0.0, 0.0, 100.0, 100.0),
        ("s0",),
        "model hypothesis",
        Confidence(None, "uncalibrated"),
    )
    raw = PagePartition(
        "layout-v2",
        page.source_manifest_id,
        page.source_sha256,
        0,
        "model",
        (item,),
        (),
    )
    reconciled = preserve_omitted_occurrences(page, raw)
    validate_partition(page, reconciled)
    assert (
        reconciled.objects == raw.objects
    )  # Proximity does not create a relationship.
    assert reconciled.unassigned_span_ids == tuple(
        f"s{n}" for n in range(1, omitted + 1)
    )
    assert len(reconciled.diagnostics) == omitted
    assert raw.unassigned_span_ids == () and item.verification == "pending"
    with pytest.raises(ValueError, match="unknown"):
        preserve_omitted_occurrences(
            page, replace(raw, unassigned_span_ids=("invented",))
        )
    with pytest.raises(ValueError, match="unique"):
        preserve_omitted_occurrences(page, replace(raw, unassigned_span_ids=("s0",)))
