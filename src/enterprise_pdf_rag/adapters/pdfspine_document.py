"""Preserve native PDF exports and independent, source-positioned text observations."""

from hashlib import sha256
from xml.etree import ElementTree

import pdfspine
from pydantic import BaseModel, ConfigDict, FiniteFloat, TypeAdapter, field_validator

from enterprise_pdf_rag.adapters.pdfspine_svg import (
    crop_native_svg,
    validate_native_svg,
)
from enterprise_pdf_rag.documents.models import (
    Bounds,
    DocumentExtraction,
    PageExtraction,
    RegionExtraction,
    TextSpan,
)

_RECORDS = TypeAdapter(list[dict[str, object]], config=ConfigDict(strict=True))
_BOUNDS = TypeAdapter(tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat])
_SVG = "http://www.w3.org/2000/svg"
_WARNINGS = (
    "Native pdfspine SVG export retained; original PDF visual completeness is not verified.",
    "Text spans are independent observations; span-to-SVG glyph mapping is not verified.",
    "Chart grammar and series/value relationships are not verified.",
)


class _PageText(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    width: FiniteFloat
    height: FiniteFloat
    blocks: list[dict[str, object]]


class _Span(BaseModel):
    """Validate selected SDK fields without claiming a SVG-glyph relationship."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    text: str
    bbox: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
    origin: tuple[FiniteFloat, FiniteFloat]
    font: str
    size: FiniteFloat
    direction: tuple[FiniteFloat, FiniteFloat]

    @field_validator("bbox")
    @classmethod
    def ordered_bbox(cls, value: Bounds) -> Bounds:
        if value[0] > value[2] or value[1] > value[3]:
            raise ValueError("Text bounds are inverted")
        return value

    @field_validator("size")
    @classmethod
    def nonnegative_size(cls, value: float) -> float:
        if value < 0:
            raise ValueError("Text size is negative")
        return value


def _spans(text: _PageText, *, source_digest: str, page_index: int) -> tuple[TextSpan, ...]:
    observations: list[TextSpan] = []
    for block_index, block in enumerate(text.blocks):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(_RECORDS.validate_python(block.get("lines"))):
            for span_index, raw in enumerate(_RECORDS.validate_python(line.get("spans"))):
                span = _Span.model_validate(
                    {
                        "text": raw.get("text"),
                        "bbox": raw.get("bbox"),
                        "origin": raw.get("origin"),
                        "font": raw.get("font"),
                        "size": raw.get("size"),
                        "direction": raw.get("dir", line.get("dir")),
                    }
                )
                occurrence = f"{source_digest}:{page_index}:{block_index}:{line_index}:{span_index}"
                span_id = f"span-v1-{sha256(occurrence.encode()).hexdigest()}"
                observations.append(
                    TextSpan(
                        span_id,
                        span.text,
                        span.bbox,
                        span.origin,
                        span.font,
                        span.size,
                        span.direction,
                    )
                )
    return tuple(observations)


def _extract_page(page: pdfspine.Page, *, source_digest: str, page_index: int) -> PageExtraction:
    bounds = _BOUNDS.validate_python(tuple(page.rect))
    if page.rotation != 0:
        raise ValueError("Rotated pages require a verified coordinate adapter")
    if tuple(page.cropbox) != tuple(page.mediabox) or bounds[:2] != (0.0, 0.0):
        raise ValueError("Non-default crop coordinates require verification")
    width, height = bounds[2], bounds[3]
    text = _PageText.model_validate(page.get_text("dict"))
    if (text.width, text.height) != (width, height):
        raise ValueError("Text and native SVG page coordinates disagree")
    native_svg = page.get_svg_image(text_as_path=False)
    validate_native_svg(native_svg, width=width, height=height)
    warnings = list(_WARNINGS)
    if ElementTree.fromstring(native_svg).findall(f".//{{{_SVG}}}image"):
        warnings.append(
            "Native SVG contains raster image assets; this is a mixed vector/raster source."
        )
    return PageExtraction(
        page_index,
        width,
        height,
        page.rotation,
        native_svg,
        _spans(text, source_digest=source_digest, page_index=page_index),
        tuple(warnings),
    )


def _checked_page(
    document: pdfspine.Document, *, source_digest: str, page_index: int
) -> PageExtraction:
    try:
        return _extract_page(
            document.load_page(page_index),
            source_digest=source_digest,
            page_index=page_index,
        )
    except (pdfspine.PdfError, OSError, ValueError, TypeError, RuntimeError) as error:
        raise ValueError(f"Page index {page_index} extraction failed: {error}") from error


def _intersects(left: Bounds, right: Bounds) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


class PdfspineDocumentAdapter:
    """Export every page or explicitly fail; source qualification remains pending."""

    def extract_document(self, pdf: bytes) -> DocumentExtraction:
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Expected PDF bytes")
        source_digest = sha256(pdf).hexdigest()
        document = pdfspine.open(stream=pdf, filetype="pdf")
        try:
            pages = tuple(
                _checked_page(document, source_digest=source_digest, page_index=page_index)
                for page_index in range(document.page_count)
            )
        finally:
            document.close()
        return DocumentExtraction(
            pages, f"pdfspine/{pdfspine.__version__}; native-svg/text-dict-v1"
        )

    def extract_region(self, pdf: bytes, *, page_index: int, bbox: Bounds) -> RegionExtraction:
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Expected PDF bytes")
        source_digest = sha256(pdf).hexdigest()
        document = pdfspine.open(stream=pdf, filetype="pdf")
        try:
            if not 0 <= page_index < document.page_count:
                raise ValueError(f"Page index {page_index} is out of range")
            page = _checked_page(document, source_digest=source_digest, page_index=page_index)
        finally:
            document.close()
        cropped = crop_native_svg(page.native_svg, width=page.width, height=page.height, bbox=bbox)
        spans = tuple(span for span in page.text_spans if _intersects(span.bbox, bbox))
        warnings = list(page.warnings)
        if any(
            not (
                bbox[0] <= span.bbox[0] <= span.bbox[2] <= bbox[2]
                and bbox[1] <= span.bbox[1] <= span.bbox[3] <= bbox[3]
            )
            for span in spans
        ):
            warnings.append(
                "Intersecting text spans retain their full page bounds and text, including parts outside the crop."
            )
        return RegionExtraction(
            page.page_index,
            page.width,
            page.height,
            page.rotation,
            bbox,
            page.native_svg,
            cropped,
            spans,
            tuple(warnings),
        )
