"""Shared page qualifiers remain distinct from figure-local source occurrences."""

from dataclasses import replace
from hashlib import sha256

import pytest

from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import (
    LayoutObject,
    ObjectKind,
    PageInput,
    PagePartition,
)
from enterprise_pdf_rag.processing.service import validate_partition


def test_page_context_requires_real_distinct_occurrences_and_parent_graph_is_acyclic() -> (
    None
):
    page = PageInput(
        "a" * 64,
        "b" * 64,
        19,
        960.0,
        540.0,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            19,
            (
                TextSpan("mark", "10%", (10.0, 10.0, 30.0, 20.0)),
                TextSpan(
                    "footer",
                    "Changes are shown at constant exchange rates.",
                    (10.0, 500.0, 400.0, 515.0),
                ),
            ),
        ),
    )
    chart = LayoutObject(
        "chart",
        ObjectKind.CHART,
        (0.0, 0.0, 100.0, 100.0),
        ("mark",),
        "chart hypothesis",
        Confidence(None, "test"),
        context_span_ids=("footer",),
    )
    partition = PagePartition(
        "layout-v2",
        page.source_manifest_id,
        page.source_sha256,
        page.page_index,
        "test",
        (chart,),
        ("footer",),
    )
    validate_partition(page, partition)
    with pytest.raises(ValueError, match="ownership"):
        validate_partition(
            page, replace(partition, unassigned_span_ids=("footer", "mark"))
        )
    assert chart.source_span_ids == ("mark",)
    for context in (("mark",), ("invented",), ("footer", "footer")):
        with pytest.raises(ValueError, match="context"):
            validate_partition(
                page,
                replace(partition, objects=(replace(chart, context_span_ids=context),)),
            )
    with pytest.raises(ValueError, match="cycle"):
        validate_partition(
            page, replace(partition, objects=(replace(chart, parent_id="chart"),))
        )
