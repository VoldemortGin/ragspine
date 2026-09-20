"""Source observations remain complete and separate from inferred object types."""

from hashlib import sha256

import pytest

from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.processing.models import PageInput, ProcessingScope
from enterprise_pdf_rag.processing.service import canonical_page


def test_twenty_page_scope_does_not_claim_seventy_one_processed_pages() -> None:
    scope = ProcessingScope("a" * 64, "b" * 64, 71, tuple(range(20)))
    assert scope.physical_pages == tuple(range(1, 21))
    assert scope.source_page_count == 71
    assert ProcessingScope("a" * 64, "b" * 64, 71, (0, 20)).physical_pages == (1, 21)
    with pytest.raises(ValueError, match="selected"):
        ProcessingScope("a" * 64, "b" * 64, 71, (0, 71))
    with pytest.raises(ValueError, match="unique"):
        ProcessingScope("a" * 64, "b" * 64, 71, (0, 0))


def test_canonical_preserves_occurrences_without_inventing_chart_relationships() -> (
    None
):
    native = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    observed = (
        TextSpan("s1", "72%", (10.0, 10.0, 30.0, 20.0)),
        TextSpan("s2", "72%", (50.0, 10.0, 70.0, 20.0)),
    )
    source = PageInput(
        "a" * 64,
        "b" * 64,
        17,
        960.0,
        540.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", "b" * 64, 17, observed),
    )
    canonical = canonical_page(source)
    assert canonical.source_manifest_id == source.source_manifest_id
    assert canonical.native_svg == source.native_svg
    assert tuple(item.source_span_id for item in canonical.text) == ("s1", "s2")
    assert canonical.text[0].element_id != canonical.text[1].element_id
    assert canonical.text[0].text == canonical.text[1].text == "72%"
    assert canonical.text[0].source.bbox != canonical.text[1].source.bbox
    assert canonical == canonical_page(source)
