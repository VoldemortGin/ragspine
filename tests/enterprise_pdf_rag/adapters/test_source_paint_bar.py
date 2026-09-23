"""Bar visibility binds real source glyphs and explicitly bounded strokes."""

from hashlib import sha256

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.source_paint_bar import (
    SourcePaintBarProof,
    build_bar_source_paint_proof,
    prove_page_context,
)
from ragspine.extraction.evidence.document.models import AssetRef, TextSidecar
from ragspine.extraction.evidence.figures.chart_qa.displayed_evidence import ACTUAL_FX_CONTEXT
from ragspine.extraction.evidence.page.models import PageInput
from tests.enterprise_pdf_rag.adapters.bar_source_fixture import bar_source
from tests.enterprise_pdf_rag.adapters.test_source_paint import glyph_source


def outlined_source() -> tuple[bytes, PreparedFigure]:
    original, _, _ = glyph_source()
    with pdfspine.open(stream=original, filetype="pdf") as document:
        document[0].draw_rect((5.0, 5.0, 95.0, 95.0), color=(0.0, 0.0, 0.0), width=2.0)
        source = document.tobytes()
    extracted = PdfspineDocumentAdapter().extract_document(source).pages[0]
    native = extracted.native_svg.encode()
    source_hash = sha256(source).hexdigest()
    return source, prepare_figure(
        page=PageInput(
            "a" * 64,
            source_hash,
            0,
            extracted.width,
            extracted.height,
            AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
            TextSidecar("source-text-v1", source_hash, 0, extracted.text_spans),
        ),
        native_svg=native,
        bbox=(0.0, 0.0, 100.0, 100.0),
        region_id="authored-stroke",
    )


def test_bar_proof_requires_a_trusted_sdk_profile_before_glyph_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, prepared, _ = glyph_source()
    monkeypatch.setattr(pdfspine.Page, "get_paint_profile", None, raising=False)

    with pytest.raises(ValueError, match="trusted_paint_profile_unavailable"):
        build_bar_source_paint_proof(source, prepared=prepared)


def test_real_glyphs_remain_visible_inside_a_later_stroked_frame() -> None:
    source, prepared = outlined_source()

    proof = build_bar_source_paint_proof(source, prepared=prepared)

    assert tuple(glyph.character for glyph in proof.glyphs) == ("7", "2", "%")
    assert len(proof.vectors) == 1
    assert proof.vectors[0].style_status == "bounded_unknown_style"
    assert proof.stroke_fidelity == "pending"
    assert proof.profile.source_sha256 == sha256(source).hexdigest()


def test_derived_libm_envelopes_do_not_enter_persisted_proof_identity() -> None:
    source, prepared = outlined_source()
    proof = build_bar_source_paint_proof(source, prepared=prepared)
    encoded = TypeAdapter(SourcePaintBarProof).dump_json(proof)
    assert b'"envelope"' not in encoded
    assert b"pdf-solid-stroke-envelope-v1" in encoded
    restored = TypeAdapter(SourcePaintBarProof).validate_json(encoded, strict=True)
    assert restored.proof_id == proof.proof_id
    assert restored.vectors[0].envelope is not None


def test_page_context_gets_its_own_full_source_and_visible_glyph_proof() -> None:
    source = bar_source()
    sidecar = TypeAdapter(TextSidecar).dump_json(source.page.text)
    span = next(span for span in source.page.text.spans if span.text == ACTUAL_FX_CONTEXT)
    native = PdfspineDocumentAdapter().extract_document(source.pdf).pages[0].native_svg.encode()
    note, proof = prove_page_context(
        source.pdf,
        page=source.page,
        native_svg=native,
        source_text=sidecar,
        source_span_id=span.span_id,
    )
    assert note.source_text_sha256 == sha256(sidecar).hexdigest()
    assert note.source.bbox == span.bbox
    assert proof.bbox != source.prepared.svg.source.bbox
    assert {glyph.source_span_id for glyph in proof.glyphs} == {span.span_id}


@pytest.mark.parametrize("option", ["hidden_footer", "cover_footer"])
def test_invisible_or_later_covered_footer_cannot_supply_page_context(
    option: str,
) -> None:
    source = bar_source(
        hidden_footer=option == "hidden_footer", cover_footer=option == "cover_footer"
    )
    native = PdfspineDocumentAdapter().extract_document(source.pdf).pages[0].native_svg.encode()
    sidecar = TypeAdapter(TextSidecar).dump_json(source.page.text)
    span = next(span for span in source.page.text.spans if span.text == ACTUAL_FX_CONTEXT)
    with pytest.raises(ValueError, match=r"unsupported_source_text_mode|source_glyph_occluded"):
        prove_page_context(
            source.pdf,
            page=source.page,
            native_svg=native,
            source_text=sidecar,
            source_span_id=span.span_id,
        )
