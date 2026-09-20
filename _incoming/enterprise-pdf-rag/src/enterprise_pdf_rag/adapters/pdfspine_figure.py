"""Traceable, deliberately partial SVG projection through pdfspine only.

The released drawing API exposes PDF bottom-left coordinates while text spans
use top-left page coordinates. This adapter normalizes explicitly. It does not
assert that pdfspine's public observations expose every paint operation.
"""

import hashlib
from collections.abc import Sequence
from math import isfinite
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from enterprise_pdf_rag.figures.models import (
    SourceAnchor,
    SvgArtifact,
    SvgElement,
    Verification,
)

type Bounds = tuple[float, float, float, float]
type Point = tuple[float, float]

_NAMESPACE = "http://www.w3.org/2000/svg"
_BOUNDS = TypeAdapter(tuple[float, float, float, float])
_POINT = TypeAdapter(tuple[float, float])
_BLOCKS = TypeAdapter(list[dict[str, object]])
_ITEMS = TypeAdapter(list[dict[str, object]])


class _Drawing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    rect: Bounds
    color: tuple[float, ...] | None
    fill: tuple[float, ...] | None
    width: float
    dashes: str
    close_path: bool = Field(alias="closePath")
    even_odd: bool
    items: tuple[tuple[object, ...], ...]


class _Span(BaseModel):
    """Explicit SDK-to-project boundary projection, not arbitrary model JSON."""

    model_config = ConfigDict(extra="forbid")
    text: str
    bbox: Bounds
    origin: Point
    size: float
    font: str
    color: int
    direction: Point


class _PageText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: float
    height: float
    blocks: list[dict[str, object]]


def _intersects(left: Bounds, right: Bounds) -> bool:
    return (
        left[0] < right[2]
        and right[0] < left[2]
        and left[1] < right[3]
        and right[1] < left[3]
    )


def _inside(inner: Bounds, outer: Bounds) -> bool:
    return (
        outer[0] <= inner[0] <= inner[2] <= outer[2]
        and outer[1] <= inner[1] <= inner[3] <= outer[3]
    )


def _number(value: float) -> str:
    if not isfinite(value):
        raise ValueError("Non-finite SVG coordinate")
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _rgb(color: Sequence[float] | None) -> str:
    if color is None:
        return "none"
    values = tuple(color) * 3 if len(color) == 1 else tuple(color)
    if len(values) != 3 or any(
        not isfinite(value) or not 0 <= value <= 1 for value in values
    ):
        raise ValueError("Unsupported drawing color space")
    return "#" + "".join(f"{round(value * 255):02x}" for value in values)


def _top_left(bounds: Bounds, height: float) -> Bounds:
    x0, y0, x1, y1 = bounds
    return min(x0, x1), height - max(y0, y1), max(x0, x1), height - min(y0, y1)


def _point(value: object, height: float) -> Point:
    x, y = _POINT.validate_python(value)
    return x, height - y


def _span_models(blocks: list[dict[str, object]]) -> list[_Span]:
    result: list[_Span] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in _BLOCKS.validate_python(block.get("lines", [])):
            for span in _BLOCKS.validate_python(line.get("spans", [])):
                # Select the documented fields this projection consumes; retained
                # source digests and raw SVG diagnostics identify the SDK result.
                result.append(
                    _Span.model_validate(
                        {
                            "text": span.get("text"),
                            "bbox": span.get("bbox"),
                            "origin": span.get("origin"),
                            "size": span.get("size"),
                            "font": span.get("font"),
                            "color": span.get("color"),
                            "direction": span.get("dir", line.get("dir")),
                        }
                    )
                )
    return result


def _draw_item(item: tuple[object, ...], height: float) -> tuple[str, dict[str, str]]:
    if len(item) == 2 and item[0] == "re":
        x0, y0, x1, y1 = _top_left(_BOUNDS.validate_python(item[1]), height)
        return "rect", {
            "x": _number(x0),
            "y": _number(y0),
            "width": _number(x1 - x0),
            "height": _number(y1 - y0),
        }
    if len(item) == 3 and item[0] == "l":
        first, last = _point(item[1], height), _point(item[2], height)
        return "path", {
            "d": f"M{_number(first[0])} {_number(first[1])} L{_number(last[0])} {_number(last[1])}"
        }
    if len(item) == 5 and item[0] == "c":
        points = [_point(point, height) for point in item[1:]]
        start = points[0]
        controls = " ".join(f"{_number(x)} {_number(y)}" for x, y in points[1:])
        return "path", {"d": f"M{_number(start[0])} {_number(start[1])} C{controls}"}
    raise ValueError("Unsupported SVG primitive")


class PdfspineFigureParser:
    """Export explicit source observations; arbitrary PDFs remain pending review."""

    def extract(self, pdf: bytes, *, page_index: int, bbox: Bounds) -> SvgArtifact:
        import pdfspine

        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Expected PDF bytes")
        if (
            not all(isfinite(v) for v in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise ValueError("Expected finite non-empty figure bounds")
        digest = hashlib.sha256(pdf).hexdigest()
        document = pdfspine.open(stream=pdf, filetype="pdf")
        try:
            if not 0 <= page_index < document.page_count:
                raise ValueError("Page index out of range")
            page = document.load_page(page_index)
            if page.rotation != 0:
                raise ValueError("Rotated pages require a verified coordinate adapter")
            page_bounds = _BOUNDS.validate_python(tuple(page.rect))
            if not _inside(bbox, page_bounds):
                raise ValueError("Figure bounds exceed the page")
            if tuple(page.cropbox) != tuple(page.mediabox) or page_bounds[:2] != (
                0.0,
                0.0,
            ):
                raise ValueError("Non-default crop coordinates require verification")
            text = _PageText.model_validate(page.get_text("dict"))
            drawings = [
                _Drawing.model_validate(value)
                for value in _ITEMS.validate_python(page.get_cdrawings())
            ]
            original_svg = ElementTree.fromstring(page.get_svg_image())
        finally:
            document.close()
        source = SourceAnchor(f"sha256:{digest}", digest, page_index, bbox)
        figure_id = (
            "figure-"
            + hashlib.sha256(f"{digest}:{page_index}:{bbox}".encode()).hexdigest()[:24]
        )
        warnings = [
            "pending: public PDF observations do not prove complete source graphics coverage"
        ]
        for name in (
            "clipPath",
            "image",
            "mask",
            "pattern",
            "linearGradient",
            "radialGradient",
        ):
            if original_svg.findall(f".//{{{_NAMESPACE}}}{name}"):
                warnings.append(f"source contains {name}; reconstruction needs review")
        if any(block.get("type") != 0 for block in text.blocks):
            warnings.append(
                "source has non-text blocks; raster coverage is not verified"
            )
        root = ElementTree.Element(
            "svg",
            {
                "xmlns": _NAMESPACE,
                "viewBox": " ".join(
                    _number(v)
                    for v in (bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
                ),
                "width": _number(bbox[2] - bbox[0]),
                "height": _number(bbox[3] - bbox[1]),
                "data-source-sha256": digest,
                "data-figure-id": figure_id,
                "data-verification": "pending",
            },
        )
        elements: list[SvgElement] = []
        for index, drawing in enumerate(drawings):
            bounds = _top_left(drawing.rect, text.height)
            # Include the stroke's area in a line's evidence rectangle.
            if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
                radius = max(drawing.width / 2, 0.01)
                bounds = (
                    bounds[0] - radius,
                    bounds[1] - radius,
                    bounds[2] + radius,
                    bounds[3] + radius,
                )
            if not _intersects(bounds, bbox):
                continue
            if drawing.dashes:
                warnings.append(f"drawing {index} has a dash pattern; not reproduced")
            if drawing.type not in {"f", "s", "fs"}:
                warnings.append(f"drawing {index} has an unsupported paint mode")
                continue
            # A path with connected segments must retain one fill topology.
            if len(drawing.items) != 1:
                warnings.append(
                    f"drawing {index} has a compound path; not reconstructed"
                )
                continue
            try:
                tag, attrs = _draw_item(drawing.items[0], text.height)
                attrs.update(
                    fill=_rgb(drawing.fill),
                    stroke=_rgb(drawing.color),
                    **{"stroke-width": _number(drawing.width)},
                )
            except ValueError:
                warnings.append(f"drawing {index} has unsupported geometry or colors")
                continue
            ident = f"drawing-{index}"
            ElementTree.SubElement(root, tag, {"id": ident, **attrs})
            elements.append(
                SvgElement(
                    ident,
                    "",
                    SourceAnchor(source.source_revision, digest, page_index, bounds),
                )
            )
        for index, span in enumerate(_span_models(text.blocks)):
            if not span.text.strip() or not _intersects(span.bbox, bbox):
                continue
            if not _inside(span.bbox, bbox) or span.direction != (1.0, 0.0):
                warnings.append(
                    f"text {index} is clipped or non-horizontal; not reconstructed"
                )
                continue
            ident = f"text-{index}"
            node = ElementTree.SubElement(
                root,
                "text",
                {
                    "id": ident,
                    "x": _number(span.origin[0]),
                    "y": _number(span.origin[1]),
                    "font-size": _number(span.size),
                    "font-family": span.font,
                    "fill": f"#{span.color:06x}",
                },
            )
            node.text = span.text
            elements.append(
                SvgElement(
                    ident,
                    span.text,
                    SourceAnchor(source.source_revision, digest, page_index, span.bbox),
                )
            )
        return SvgArtifact(
            figure_id,
            source,
            ElementTree.tostring(root, encoding="unicode"),
            tuple(elements),
            verification=Verification.PENDING,
            warnings=tuple(warnings),
        )
