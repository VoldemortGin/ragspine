"""The model view renders saved SVG only, with fixed fonts and no external I/O."""

from hashlib import sha256

import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.figure_reasoning import prepare_figure, render_svg_png
from ragspine.extraction.evidence.document.models import AssetRef, TextSidecar, TextSpan
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.figures.validation import validate_svg
from ragspine.extraction.evidence.page.models import PageInput

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"><path d="M10 10H90V40H10Z" fill="#dc0046"/></svg>'


def test_svg_render_is_bounded_deterministic_and_names_its_renderer() -> None:
    result = render_svg_png(SVG, width=200)
    again = render_svg_png(SVG, width=200)
    assert result == again
    assert result.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (result.width, result.height) == (200, 100)
    assert "resvg" in result.renderer_fingerprint
    assert "system-fonts=false" in result.renderer_fingerprint


@pytest.mark.parametrize(
    "href",
    ["https://example.invalid/image.png", "file:///private/image.png", "../image.png"],
)
def test_svg_external_resources_are_rejected_before_render(href: str) -> None:
    value = SVG.replace(b"</svg>", f'<image href="{href}"/></svg>'.encode())
    with pytest.raises(ValueError, match="external"):
        render_svg_png(value)


def test_case_insensitive_style_import_is_rejected_before_render() -> None:
    source = SVG.replace(
        b"</svg>", b'<style>@IMPORT "https://example.invalid/font.css";</style></svg>'
    )
    with pytest.raises(ValueError, match="external"):
        render_svg_png(source)


def test_model_view_binds_saved_svg_and_excludes_partially_intersecting_observations() -> None:
    source_sha = sha256(b"source").hexdigest()
    text = TextSidecar(
        "source-text-v1",
        source_sha,
        17,
        (
            TextSpan("value-span", "72%", (20.0, 20.0, 40.0, 30.0)),
            TextSpan("clipped-neighbour", "Other", (70.0, 20.0, 95.0, 30.0)),
        ),
    )
    page = PageInput(
        "a" * 64,
        source_sha,
        17,
        100.0,
        50.0,
        AssetRef(sha256(SVG).hexdigest(), "image/svg+xml", len(SVG)),
        text,
    )
    result = prepare_figure(
        page=page,
        native_svg=SVG,
        bbox=(10.0, 10.0, 80.0, 45.0),
        region_id="test-region",
    )
    assert result.svg.verification is Verification.PENDING
    assert result.view.native_svg_digest == sha256(SVG).hexdigest()
    assert result.view.svg_digest == result.svg.digest
    assert result.view.render_digest == result.rendered.digest
    assert result.excluded_span_ids == ("clipped-neighbour",)
    assert all(element.source_span_id == "value-span" for element in result.svg.elements)
    assert {element.text for element in result.svg.elements} == {"72%", "72", "%"}
    assert all(
        element.evidence_kind.value == "source_text_observation" for element in result.svg.elements
    )
    validate_svg(result.svg)


@pytest.mark.parametrize("blank", ["", " ", "\t \n"])
def test_whitespace_spans_do_not_invent_reversed_unicode_ranges(blank: str) -> None:
    source_sha = sha256(b"source-with-whitespace").hexdigest()
    spans = (
        TextSpan("blank", blank, (5.0, 5.0, 9.0, 9.0)),
        TextSpan("value", " 72% ", (20.0, 20.0, 40.0, 30.0)),
    )
    page = PageInput(
        "a" * 64,
        source_sha,
        0,
        100.0,
        50.0,
        AssetRef(sha256(SVG).hexdigest(), "image/svg+xml", len(SVG)),
        TextSidecar("source-text-v1", source_sha, 0, spans),
    )
    prepared = prepare_figure(
        page=page, native_svg=SVG, bbox=(0.0, 0.0, 100.0, 40.0), region_id="blank"
    )
    assert page.text.spans[0] == spans[0]
    assert all(element.source_span_id == "value" for element in prepared.svg.elements)
    assert {(element.text, element.text_range) for element in prepared.svg.elements} == {
        ("72%", (1, 4)),
        ("72", (1, 3)),
        ("%", (3, 4)),
    }
    validate_svg(prepared.svg)


def test_page_context_is_separately_bound_and_empty_context_keeps_the_old_view() -> None:
    source_sha = sha256(b"context-source").hexdigest()
    text = TextSidecar(
        "source-text-v1",
        source_sha,
        19,
        (
            TextSpan("value", "742", (10.0, 10.0, 30.0, 20.0)),
            TextSpan(
                "footer",
                "Changes use constant FX; bar values use actual FX.",
                (5.0, 44.0, 95.0, 49.0),
            ),
        ),
    )
    page = PageInput(
        "a" * 64,
        source_sha,
        19,
        100.0,
        50.0,
        AssetRef(sha256(SVG).hexdigest(), "image/svg+xml", len(SVG)),
        text,
    )
    bare = prepare_figure(page=page, native_svg=SVG, bbox=(0.0, 0.0, 100.0, 40.0), region_id="fx")
    empty = prepare_figure(
        page=page,
        native_svg=SVG,
        bbox=(0.0, 0.0, 100.0, 40.0),
        region_id="fx",
        context_span_ids=(),
    )
    contextual = prepare_figure(
        page=page,
        native_svg=SVG,
        bbox=(0.0, 0.0, 100.0, 40.0),
        region_id="fx",
        context_span_ids=("footer",),
    )
    assert bare == empty
    assert bare.model_view_id == bare.view.artifact_id
    assert bare.model_view is bare.view
    assert contextual.svg == bare.svg and contextual.rendered == bare.rendered
    assert contextual.model_view_id != bare.model_view_id
    (context,) = contextual.page_context
    assert context.text == text.spans[1].text and context.source_span_id == "footer"
    assert context.source.document_sha256 == source_sha and context.source.page_index == 19
    assert context.scope == "page_context"
    assert all(element.source_span_id != "footer" for element in contextual.svg.elements)
    encoded = TypeAdapter(type(contextual.model_view)).dump_json(contextual.model_view)
    assert b"constant FX" in encoded and b"page_context" in encoded


@pytest.mark.parametrize(
    "context_ids", [("missing",), ("local",), ("partial",), ("footer", "footer")]
)
def test_unknown_duplicate_or_locally_overlapping_context_is_rejected(
    context_ids: tuple[str, ...],
) -> None:
    source_sha = sha256(b"context-source").hexdigest()
    spans = (
        TextSpan("local", "742", (10.0, 10.0, 30.0, 20.0)),
        TextSpan("partial", "Neighbor", (20.0, 35.0, 40.0, 45.0)),
        TextSpan("footer", "Actual FX.", (5.0, 46.0, 95.0, 49.0)),
    )
    page = PageInput(
        "a" * 64,
        source_sha,
        19,
        100.0,
        50.0,
        AssetRef(sha256(SVG).hexdigest(), "image/svg+xml", len(SVG)),
        TextSidecar("source-text-v1", source_sha, 19, spans),
    )
    with pytest.raises(ValueError, match="Page context"):
        prepare_figure(
            page=page,
            native_svg=SVG,
            bbox=(0.0, 0.0, 100.0, 40.0),
            region_id="context",
            context_span_ids=context_ids,
        )
