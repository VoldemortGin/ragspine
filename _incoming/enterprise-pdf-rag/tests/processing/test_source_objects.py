"""Literal text projections can be grounded without inventing financial facts."""

from dataclasses import replace
from hashlib import sha256

import pytest

from enterprise_pdf_rag.adapters.source_objects import source_object_ir
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.typed_ir import ListIR


def test_text_list_and_group_ir_preserve_literal_source_without_claiming_relations() -> (
    None
):
    page = PageInput(
        "a" * 64,
        "b" * 64,
        3,
        960.0,
        540.0,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            3,
            (
                TextSpan("s0", "Operating ROE", (10.0, 10.0, 50.0, 20.0)),
                TextSpan("s1", "17.5%", (10.0, 30.0, 50.0, 40.0)),
            ),
        ),
    )
    for kind in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP):
        item = LayoutObject(
            "metric",
            kind,
            (0.0, 0.0, 100.0, 100.0),
            ("s0", "s1"),
            "Inferred layout",
            Confidence(None, "test inference"),
        )
        result = source_object_ir(page, item)
        assert result.description.text == "Operating ROE\n17.5%"
        assert result.description.source_span_ids == ("s0", "s1")
        assert result.description.producer == "exact-source-transcription-v1"
        assert result.kind is kind and result.source.page_index == 3
        assert result.description.verification == "verified"
        assert result.classification_verification == "pending"
        assert "for" not in result.description.text
        if kind is ObjectKind.LIST:
            structured = source_object_ir(
                page,
                replace(item, list_item_span_ids=(("s0", "s1"),), list_ordered=False),
            )
            assert isinstance(structured.ir, ListIR)
            assert structured.ir.item_groups == (("s0", "s1"),)


def test_nonchart_projection_rejects_unobserved_or_outside_text_occurrences() -> None:
    page = PageInput(
        "a" * 64,
        "b" * 64,
        3,
        960.0,
        540.0,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar(
            "source-text-v1",
            "b" * 64,
            3,
            (TextSpan("outside", "same value", (200.0, 200.0, 250.0, 210.0)),),
        ),
    )
    for span_id in ("missing", "outside"):
        item = LayoutObject(
            "text",
            ObjectKind.TEXT,
            (0.0, 0.0, 100.0, 100.0),
            (span_id,),
            "Inferred layout",
            Confidence(None, "test inference"),
        )
        with pytest.raises(ValueError, match="occurrence"):
            source_object_ir(page, item)
