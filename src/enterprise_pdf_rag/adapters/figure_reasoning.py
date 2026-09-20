"""Bounded SVG-derived model views. This module never opens or parses a PDF."""

import re
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from math import isfinite
from typing import Literal
from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.documents.models import Bounds, TextSpan
from enterprise_pdf_rag.figures.models import (
    EvidenceKind,
    SourceAnchor,
    SvgArtifact,
    SvgElement,
    content_id,
)
from enterprise_pdf_rag.processing.models import PageInput

_SVG = "http://www.w3.org/2000/svg"


@dataclass(frozen=True, slots=True)
class RenderedSvg:
    png: bytes
    width: int
    height: int
    renderer_fingerprint: str

    @property
    def digest(self) -> str:
        return sha256(self.png).hexdigest()


@dataclass(frozen=True, slots=True)
class FigureModelView:
    source_manifest_id: str
    source_sha256: str
    page_index: int
    bbox: Bounds
    native_svg_digest: str
    crop_svg_digest: str
    svg_digest: str
    render_digest: str
    renderer_fingerprint: str

    @property
    def artifact_id(self) -> str:
        return content_id("figure-model-view-v1", (self,))


@dataclass(frozen=True, slots=True)
class PageContextObservation:
    source_span_id: str
    text: str
    source: SourceAnchor
    scope: Literal["page_context"] = "page_context"

    @property
    def observation_id(self) -> str:
        return content_id("page-context-observation-v1", (self,))


@dataclass(frozen=True, slots=True)
class ContextualFigureModelView:
    base_view: FigureModelView
    page_context: tuple[PageContextObservation, ...]

    @property
    def artifact_id(self) -> str:
        return content_id(
            "contextual-figure-model-view-v1",
            (self.base_view.artifact_id, self.page_context),
        )


@dataclass(frozen=True, slots=True)
class PreparedFigure:
    svg: SvgArtifact
    crop_svg: str
    rendered: RenderedSvg
    view: FigureModelView
    excluded_span_ids: tuple[str, ...]
    page_context: tuple[PageContextObservation, ...] = ()
    paint_text_spans: tuple[TextSpan, ...] = ()

    @property
    def model_view(self) -> FigureModelView | ContextualFigureModelView:
        if not self.page_context:
            return self.view
        return ContextualFigureModelView(self.view, self.page_context)

    @property
    def model_view_id(self) -> str:
        return self.model_view.artifact_id

    @property
    def context_artifact_id(self) -> str | None:
        return content_id("page-context-v1", self.page_context) if self.page_context else None


def _inside(inner: Bounds, outer: Bounds) -> bool:
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _fragments(span: TextSpan) -> tuple[tuple[int, int], ...]:
    start = len(span.text) - len(span.text.lstrip())
    end = len(span.text.rstrip())
    if start >= end:
        return ()
    ranges = [(start, end)]
    if re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?%", span.text[start:end]):
        ranges.extend(((start, end - 1), (end - 1, end)))
    return tuple(ranges)


def prepare_figure(
    *,
    page: PageInput,
    native_svg: bytes,
    bbox: Bounds,
    region_id: str,
    context_span_ids: tuple[str, ...] = (),
) -> PreparedFigure:
    """Bind a render and fully-contained PDF text observations to the saved SVG.

    Metadata nodes are source observations, never a claim that PDF text maps to
    native glyph paths. A fragment retains its full source-span bounding box.
    """
    if (
        not region_id
        or sha256(native_svg).hexdigest() != page.native_svg.sha256
        or len(native_svg) != page.native_svg.byte_length
    ):
        raise ValueError("Saved SVG digest or region identity mismatch")
    if page.text.coordinate_frame != "page-top-left-points":
        raise ValueError("Unsupported source text coordinate frame")
    spans = {span.span_id: span for span in page.text.spans}
    if len(set(context_span_ids)) != len(context_span_ids) or any(
        span_id not in spans for span_id in context_span_ids
    ):
        raise ValueError("Page context requires unique known source occurrences")
    context: list[PageContextObservation] = []
    for span_id in context_span_ids:
        span = spans[span_id]
        if not _inside(span.bbox, (0.0, 0.0, page.width, page.height)) or (
            max(span.bbox[0], bbox[0]) < min(span.bbox[2], bbox[2])
            and max(span.bbox[1], bbox[1]) < min(span.bbox[3], bbox[3])
        ):
            raise ValueError("Page context must be on the source page and outside the crop")
        context.append(
            PageContextObservation(
                span.span_id,
                span.text,
                SourceAnchor(
                    page.source_sha256,
                    page.source_sha256,
                    page.page_index,
                    span.bbox,
                    "page-top-left-points",
                ),
            )
        )
    crop = crop_native_svg(
        native_svg.decode("utf-8"), width=page.width, height=page.height, bbox=bbox
    )
    source = SourceAnchor(
        page.source_sha256,
        page.source_sha256,
        page.page_index,
        bbox,
        "page-top-left-points",
    )
    elements: list[SvgElement] = []
    metadata: list[str] = []
    excluded: list[str] = []
    for span in page.text.spans:
        if not _inside(span.bbox, bbox):
            if max(span.bbox[0], bbox[0]) < min(span.bbox[2], bbox[2]) and max(
                span.bbox[1], bbox[1]
            ) < min(span.bbox[3], bbox[3]):
                excluded.append(span.span_id)
            continue
        anchor = SourceAnchor(
            source.source_revision,
            source.document_sha256,
            source.page_index,
            span.bbox,
            source.coordinate_frame,
        )
        for start, end in _fragments(span):
            element_id = "obs-" + sha256(f"{span.span_id}:{start}:{end}".encode()).hexdigest()[:16]
            text = span.text[start:end]
            elements.append(
                SvgElement(
                    element_id,
                    text,
                    anchor,
                    EvidenceKind.SOURCE_TEXT_OBSERVATION,
                    span.span_id,
                    (start, end),
                )
            )
            metadata.append(
                f'<observation xmlns="urn:enterprise-pdf-rag:source-observation-v1" id="{element_id}" source-span-id="{escape(span.span_id, quote=True)}" start="{start}" end="{end}">{escape(text)}</observation>'
            )
    if not elements:
        raise ValueError("Figure has no fully-contained readable source observations")
    structured = crop[:-6] + "<metadata>" + "".join(metadata) + "</metadata></svg>"
    svg = SvgArtifact(
        content_id("figure-v1", (page.source_sha256, page.page_index, bbox, region_id)),
        source,
        structured,
        tuple(elements),
        warnings=(
            "Source text observations do not assert native glyph correspondence",
            "Visual completeness and semantic relationships are unverified",
        ),
    )
    rendered = render_svg_png(
        structured.encode(), width=min(960, max(1, round((bbox[2] - bbox[0]) * 2)))
    )
    view = FigureModelView(
        page.source_manifest_id,
        page.source_sha256,
        page.page_index,
        bbox,
        page.native_svg.sha256,
        sha256(crop.encode()).hexdigest(),
        svg.digest,
        rendered.digest,
        rendered.renderer_fingerprint,
    )
    paint_spans = tuple(
        span for span in page.text.spans if _inside(span.bbox, bbox) or span.span_id in excluded
    )
    return PreparedFigure(svg, crop, rendered, view, tuple(excluded), tuple(context), paint_spans)


def _safe_svg(native_svg: bytes, width: int) -> str:
    if not 1 <= width <= 1600 or len(native_svg) > 8_000_000:
        raise ValueError("SVG render input budget exceeded")
    source = native_svg.decode("utf-8")
    if "<!DOCTYPE" in source or "<!ENTITY" in source:
        raise ValueError("SVG external declarations are forbidden")
    root = ElementTree.fromstring(source)
    if root.tag != f"{{{_SVG}}}svg":
        raise ValueError("Expected a standalone SVG")
    bounds = tuple(float(value) for value in root.attrib["viewBox"].split())
    if len(bounds) != 4 or not all(isfinite(value) for value in bounds) or min(bounds[2:]) <= 0:
        raise ValueError("Invalid SVG viewport")
    if width * width * bounds[3] / bounds[2] > 4_000_000:
        raise ValueError("SVG render pixel budget exceeded")
    for node in root.iter():
        if node.tag in {
            f"{{{_SVG}}}script",
            f"{{{_SVG}}}foreignObject",
            f"{{{_SVG}}}text",
        }:
            raise ValueError(
                "SVG scripts, foreign content or unresolved text fonts are unsupported"
            )
        for name, value in node.attrib.items():
            if name.rsplit("}", 1)[-1] == "href" and not value.startswith(
                ("#", "data:image/png;base64,", "data:image/jpeg;base64,")
            ):
                raise ValueError("SVG external resources are forbidden")
        values = " ".join(node.attrib.values()) + (node.text or "")
        if "@import" in values.lower() or any(
            not match.strip(" \"'").startswith("#")
            for match in re.findall(r"url\(([^)]*)\)", values, re.IGNORECASE)
        ):
            raise ValueError("SVG external style resources are forbidden")
    return source


def render_svg_png(native_svg: bytes, *, width: int = 960) -> RenderedSvg:
    """Render a saved SVG with explicit dimensions and system fonts disabled."""
    import resvg_py

    source = _safe_svg(native_svg, width)
    png = resvg_py.svg_to_bytes(
        svg_string=source, width=width, background="white", skip_system_fonts=True
    )
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
        raise ValueError("SVG renderer did not return PNG")
    actual_width = int.from_bytes(png[16:20], "big")
    actual_height = int.from_bytes(png[20:24], "big")
    if actual_width != width or actual_width * actual_height > 4_000_000:
        raise ValueError("SVG renderer exceeded its output dimension budget")
    fingerprint = f"resvg-py/{resvg_py.__version__};resvg/{resvg_py.__resvg_version__};width={width};background=white;system-fonts=false"
    return RenderedSvg(png, actual_width, actual_height, fingerprint)
