"""Source glyph roles must be proved from real embedded font outlines."""

from dataclasses import replace
from hashlib import sha256
from math import pi
from pathlib import Path
from typing import Protocol, cast

import pdfspine
import pytest
from pydantic import TypeAdapter
from tests.adapters.test_donut_qualification import _sector, sample

from enterprise_pdf_rag.adapters import source_paint
from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.source_paint import (
    build_source_paint_proof,
    verify_source_paint_proof,
)
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Evidence,
    FigureError,
    TextDescription,
    Verification,
)
from enterprise_pdf_rag.processing.models import PageInput


class _FontInsertionPage(Protocol):
    def insert_font(self, *, fontname: str, fontbuffer: bytes) -> int: ...


def authored_font() -> bytes:
    # Self-authored four-glyph geometric font; no third-party font/source data.
    return (
        Path(__file__)
        .parents[1]
        .joinpath("fixtures/authored-label-font.ttf")
        .read_bytes()
    )


def glyph_source(
    *, overlay: bool = False, clipped: bool = False, invisible: bool = False
) -> tuple[bytes, PreparedFigure, bytes]:
    font = authored_font()
    with pdfspine.open() as document:
        page = document.new_page(width=100, height=100)
        insert_font = cast(_FontInsertionPage, page).insert_font
        insert_font(fontname="Authored", fontbuffer=font)
        page.insert_text((20, 40), "72%", fontname="Authored", fontsize=10)
        contents = page.get_contents()
        if clipped:
            document.update_stream(
                contents[0],
                b"q\n20 50 2 20 re W n\n"
                + document.xref_stream(contents[0])
                + b"\nQ\n",
            )
        if invisible:
            document.update_stream(
                contents[0],
                document.xref_stream(contents[0]).replace(b"BT", b"BT\n3 Tr"),
            )
        if overlay:
            page.draw_rect((20, 30, 40, 42), color=None, fill=(1, 1, 1))
        source = document.tobytes()
    extracted = PdfspineDocumentAdapter().extract_document(source).pages[0]
    native = extracted.native_svg.encode()
    digest = sha256(source).hexdigest()
    page_input = PageInput(
        "a" * 64,
        digest,
        0,
        extracted.width,
        extracted.height,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", digest, 0, extracted.text_spans),
    )
    return (
        source,
        prepare_figure(
            page=page_input,
            native_svg=native,
            bbox=(0.0, 0.0, 100.0, 100.0),
            region_id="authored-label",
        ),
        font,
    )


def test_source_glyph_roles_match_embedded_outlines_in_the_saved_svg() -> None:
    source, prepared, font = glyph_source()

    proof = build_source_paint_proof(source, prepared=prepared)

    assert proof.source_sha256 == sha256(source).hexdigest()
    assert proof.native_svg_digest == prepared.view.native_svg_digest
    assert tuple(glyph.character for glyph in proof.glyphs) == ("7", "2", "%")
    assert len({glyph.native_path_ref for glyph in proof.glyphs}) == 3
    assert {glyph.font_sha256 for glyph in proof.glyphs} == {sha256(font).hexdigest()}
    assert all(glyph.source_span_id is not None for glyph in proof.glyphs)
    assert proof.coverage == "complete_source_paint"


def test_later_source_paint_cannot_cover_a_proved_glyph() -> None:
    source, prepared, _ = glyph_source(overlay=True)

    with pytest.raises(ValueError, match="source_glyph_occluded_by_later_paint"):
        build_source_paint_proof(source, prepared=prepared)


def test_same_native_digest_does_not_authorize_a_modified_crop() -> None:
    source, prepared, _ = glyph_source()
    altered = prepared.crop_svg.replace(
        "</svg>", '<path d="M20 30L40 30L40 42L20 42Z" fill="#fff"/></svg>', 1
    )
    tampered = replace(prepared, crop_svg=altered)

    with pytest.raises(ValueError, match="source_crop_derivation_mismatch"):
        build_source_paint_proof(source, prepared=tampered)


def test_pdf_clip_cannot_turn_a_partly_visible_numeric_label_into_exact_evidence() -> (
    None
):
    source, prepared, _ = glyph_source(clipped=True)

    with pytest.raises(ValueError, match="source_glyph_clipped"):
        build_source_paint_proof(source, prepared=prepared)


def test_invisible_pdf_text_cannot_become_observed_numeric_evidence() -> None:
    source, prepared, _ = glyph_source(invisible=True)

    with pytest.raises(ValueError, match="unsupported_source_text_mode"):
        build_source_paint_proof(source, prepared=prepared)


def test_serialized_proof_is_rebuilt_not_trusted_as_an_approval() -> None:
    source, prepared, _ = glyph_source()
    proof = build_source_paint_proof(source, prepared=prepared)
    changed = replace(
        proof, glyphs=(replace(proof.glyphs[0], character="9"), *proof.glyphs[1:])
    )

    with pytest.raises(ValueError, match="source_paint_proof_revalidation_mismatch"):
        verify_source_paint_proof(source, prepared=prepared, proof=changed)

    assert verify_source_paint_proof(source, prepared=prepared, proof=proof) == proof


def test_reviewed_sdk_upgrade_revalidates_the_entire_old_proof_identity() -> None:
    source, prepared, _ = glyph_source()
    current = build_source_paint_proof(source, prepared=prepared)
    old = replace(
        current,
        producer=current.producer.replace("pdfspine/0.11.0;", "pdfspine/0.10.0;"),
    )

    assert verify_source_paint_proof(source, prepared=prepared, proof=old) == old
    for changed in (
        replace(old, producer=old.producer.replace("0.10.0", "0.9.0")),
        replace(old, trace_digest="0" * 64),
        replace(old, glyphs=(replace(old.glyphs[0], character="9"), *old.glyphs[1:])),
        replace(
            old,
            glyphs=(
                replace(old.glyphs[0], clips=((0.0, 0.0, 1.0, 1.0),)),
                *old.glyphs[1:],
            ),
        ),
    ):
        with pytest.raises(
            ValueError, match="source_paint_proof_revalidation_mismatch"
        ):
            verify_source_paint_proof(source, prepared=prepared, proof=changed)


def test_legacy_proof_cannot_bypass_a_missing_trusted_source_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, prepared, _ = glyph_source()
    current = build_source_paint_proof(source, prepared=prepared)
    old = replace(
        current,
        producer=current.producer.replace("pdfspine/0.11.0;", "pdfspine/0.10.0;"),
    )
    source_paint._build_source_paint_proof.cache_clear()
    monkeypatch.setattr(pdfspine.Page, "get_paint_profile", None)
    with pytest.raises(ValueError, match="trusted_paint_profile_unavailable"):
        verify_source_paint_proof(source, prepared=prepared, proof=old)


def test_legacy_vector_fill_rule_is_revalidated_without_tolerance() -> None:
    source, prepared, _, _ = authored_donut()
    current = build_source_paint_proof(source, prepared=prepared)
    old = replace(
        current,
        producer=current.producer.replace("pdfspine/0.11.0;", "pdfspine/0.10.0;"),
    )
    changed = replace(
        old, vectors=(replace(old.vectors[0], fill_rule="nonzero"), *old.vectors[1:])
    )
    with pytest.raises(ValueError, match="source_paint_proof_revalidation_mismatch"):
        verify_source_paint_proof(source, prepared=prepared, proof=changed)


def test_replay_fill_rule_disagreement_cannot_pass_paint_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, prepared, _, _ = authored_donut()
    original = source_paint._source_events

    def flipped(page: pdfspine.Page) -> tuple[source_paint._ReplayEvent, ...]:
        return tuple(
            event.model_copy(update={"payload": {**event.payload, "even_odd": False}})
            if event.kind == "fill"
            else event
            for event in original(page)
        )

    source_paint._build_source_paint_proof.cache_clear()
    monkeypatch.setattr(source_paint, "_source_events", flipped)
    with pytest.raises(ValueError, match="source_vector_does_not_match_native_svg"):
        build_source_paint_proof(source, prepared=prepared)


def test_process_cache_reuses_exact_source_inputs_but_not_a_changed_crop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, prepared, _ = glyph_source()
    original = pdfspine.open
    calls = 0

    def counted(*, stream: bytes, filetype: str) -> pdfspine.Document:
        nonlocal calls
        calls += 1
        return original(stream=stream, filetype=filetype)

    source_paint._build_source_paint_proof.cache_clear()
    monkeypatch.setattr(pdfspine, "open", counted)
    proof = build_source_paint_proof(source, prepared=prepared)
    assert verify_source_paint_proof(source, prepared=prepared, proof=proof) == proof
    assert calls == 1
    with pytest.raises(ValueError, match="source_crop_derivation_mismatch"):
        build_source_paint_proof(
            source, prepared=replace(prepared, crop_svg=prepared.crop_svg + " ")
        )
    assert calls == 2


def authored_donut(
    *, fill_rule: str = "f*", number: str = "72%"
) -> tuple[bytes, PreparedFigure, ChartIR, TextDescription]:
    font = (
        Path(__file__)
        .parents[1]
        .joinpath("fixtures/authored-donut-ascii.ttf")
        .read_bytes()
    )
    split = pi * 0.28
    from enterprise_pdf_rag.adapters.source_paint import _commands

    def pdf_path(value: str) -> str:
        return "\n".join(
            (
                " ".join(str(v) for v in values)
                + " "
                + {"M": "m", "L": "l", "C": "c", "Z": "h"}[kind]
            ).strip()
            for kind, values in _commands(value)
        )

    commands = f"q\n1 0 0 -1 0 160 cm\n.827 .067 .271 rg\n{pdf_path(_sector(split, 2 * pi - split))}\n{fill_rule}\n.2 .239 .278 rg\n{pdf_path(_sector(2 * pi - split, 2 * pi + split))}\n{fill_rule}\nQ\n"
    with pdfspine.open() as doc:
        page = doc.new_page(width=240, height=160)
        stream = doc.get_new_xref()
        doc.update_object(stream, "<<>>")
        doc.update_stream(stream, commands.encode())
        page.set_contents(stream)
        cast(_FontInsertionPage, page).insert_font(fontname="Authored", fontbuffer=font)
        for text, point, size in (
            ("Distribution Mix", (50, 22), 9),
            ("VONB", (88, 77), 5),
            ("1H26", (89, 88), 5),
            ("Agency", (3, 83), 7),
            (number, (55, 83), 7),
            ("28%", (130, 83), 7),
            ("Partnerships", (160, 83), 7),
        ):
            page.insert_text(point, text, fontname="Authored", fontsize=size)
        source = doc.tobytes()
    extracted = PdfspineDocumentAdapter().extract_document(source).pages[0]
    native = extracted.native_svg.encode()
    digest = sha256(source).hexdigest()
    page_input = PageInput(
        "a" * 64,
        digest,
        0,
        240.0,
        160.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", digest, 0, extracted.text_spans),
    )
    prepared = prepare_figure(
        page=page_input,
        native_svg=native,
        bbox=(0.0, 0.0, 240.0, 150.0),
        region_id="authored-donut",
    )
    old, chart, description = sample()
    old_elements = {element.element_id: element for element in old.svg.elements}

    def ev(evidence: Evidence) -> Evidence:
        new_ids = []
        for identifier in evidence.element_ids:
            old_element = old_elements[identifier]
            old_span = next(
                span
                for span in old.paint_text_spans
                if span.span_id == old_element.source_span_id
            )
            target_span = next(
                span
                for span in prepared.paint_text_spans
                if span.text == (number if old_span.text == "72%" else old_span.text)
            )
            new_ids.append(
                next(
                    (
                        element.element_id
                        for element in prepared.svg.elements
                        if element.source_span_id == target_span.span_id
                        and element.text == old_element.text
                    ),
                    next(
                        element.element_id
                        for element in prepared.svg.elements
                        if element.source_span_id == target_span.span_id
                        and element.text == target_span.text
                    ),
                )
            )
        return replace(evidence, element_ids=tuple(new_ids))

    chart = replace(
        chart,
        binding=prepared.svg.binding,
        points=tuple(
            replace(
                p,
                series=replace(p.series, evidence=ev(p.series.evidence)),
                category=replace(p.category, evidence=ev(p.category.evidence)),
                unit=replace(p.unit, evidence=ev(p.unit.evidence)),
                value=replace(p.value, evidence=ev(p.value.evidence)),
            )
            for p in chart.points
        ),
        title=replace(chart.title, evidence=ev(chart.title.evidence))
        if chart.title
        else None,
        period=replace(chart.period, evidence=ev(chart.period.evidence))
        if chart.period
        else None,
        marks=(),
    )
    description = replace(
        description,
        binding=prepared.svg.binding,
        claims=tuple(replace(c, evidence=ev(c.evidence)) for c in description.claims),
    )
    return source, prepared, chart, description


def test_authored_real_pdf_donut_has_a_complete_source_proof_and_numeric_positive() -> (
    None
):
    source, prepared, chart, description = authored_donut()
    proof = build_source_paint_proof(source, prepared=prepared)
    pair = DonutQualification(prepared, source_paint=proof).qualify_pair(
        prepared.svg, chart, description
    )
    assert pair.chart.verification is Verification.VERIFIED
    assert {
        point.category.text: str(point.value.value) for point in pair.chart.points
    } == {"Agency": "72", "Partnerships": "28"}


def test_numeric_proof_rejects_nonzero_fill_rule_outside_its_supported_policy() -> None:
    source, prepared, chart, description = authored_donut(fill_rule="f")
    proof = build_source_paint_proof(source, prepared=prepared)
    with pytest.raises(FigureError, match="unsupported_source_sector_fill_rule"):
        DonutQualification(prepared, source_paint=proof).qualify_pair(
            prepared.svg, chart, description
        )


@pytest.mark.parametrize("literal", [">72%", "<72%", "~72%", "c.72%"])
def test_full_source_literal_prevents_exact_numeric_substring_cherry_picking(
    literal: str,
) -> None:
    source, prepared, chart, description = authored_donut(number=literal)
    proof = build_source_paint_proof(source, prepared=prepared)
    with pytest.raises(FigureError, match="two_explicit_percent_labels_required"):
        DonutQualification(prepared, source_paint=proof).qualify_pair(
            prepared.svg, chart, description
        )


def test_real_p18_glyph_mapping_uses_source_font_program_not_a_paint_whitelist() -> (
    None
):
    path = Path("data/samples/aia-group-2026-interim-results-presentation.pdf")
    if not path.is_file():
        pytest.skip("Pinned optional AIA source absent; no download is performed")
    source = path.read_bytes()
    digest = sha256(source).hexdigest()
    assert digest == "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
    region = PdfspineDocumentAdapter().extract_region(
        source, page_index=17, bbox=(18.0, 155.0, 250.0, 338.0)
    )
    native = region.native_svg.encode()
    page = PageInput(
        "a" * 64,
        digest,
        17,
        region.width,
        region.height,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", digest, 17, region.text_spans),
    )
    prepared = prepare_figure(
        page=page,
        native_svg=native,
        bbox=region.bbox,
        region_id="aia-p018-distribution-mix-v1",
    )

    proof = build_source_paint_proof(source, prepared=prepared)

    by_span = {
        span.span_id: "".join(
            glyph.character
            for glyph in proof.glyphs
            if glyph.source_span_id == span.span_id
        )
        for span in region.text_spans
    }
    assert {
        span.text.strip(): by_span[span.span_id]
        for span in region.text_spans
        if span.text.strip() in {"72%", "28%", "Agency", "Partnerships", "VONB", "1H26"}
    } == {
        label: label
        for label in ("72%", "28%", "Agency", "Partnerships", "VONB", "1H26")
    }
    assert len(proof.glyphs) == 48
    assert {glyph.font_sha256 for glyph in proof.glyphs} == {
        "2928106ad5657dfbee3286f01015bb0cccf652eddea1e2615a85fdb3568facdc"
    }
    object_dir = Path(
        "data/output/aia-2026-interim/pages-001-020/runs/89a3a92ca1c82354d034679d9a2c2deebe5016ef8b1194b4c1db044fee5f2d99/page-018/objects/object-8cd554cca0937deab1a7"
    )
    if not object_dir.is_dir():
        pytest.skip(
            "Pinned optional raw p18 branches absent; source glyph proof passed"
        )
    chart = TypeAdapter(ChartIR).validate_json(
        (object_dir / "ir.json").read_bytes(), strict=True
    )
    description = TypeAdapter(TextDescription).validate_json(
        (object_dir / "description.json").read_bytes(), strict=True
    )
    pair = DonutQualification(prepared, source_paint=proof).qualify_pair(
        prepared.svg, chart, description
    )
    assert pair.chart.verification is Verification.VERIFIED
    assert {
        (point.category.text, str(point.value.value)) for point in pair.chart.points
    } == {("Agency", "72"), ("Partnerships", "28")}
