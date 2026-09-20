"""Viewport crops preserve the complete native SVG subtree without redrawing."""

import re
from math import isfinite
from xml.etree import ElementTree


def validate_native_svg(native_svg: str, *, width: float, height: float) -> None:
    """Require the native exporter to use the supported unrotated page frame."""
    if not all(isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("Page dimensions must be finite and positive")
    if "<!DOCTYPE" in native_svg or "<!ENTITY" in native_svg:
        raise ValueError("Native SVG declarations are unsupported")
    try:
        root = ElementTree.fromstring(native_svg)
        view_box = tuple(float(value) for value in root.attrib["viewBox"].split())
        size = (float(root.attrib["width"]), float(root.attrib["height"]))
        offset = (float(root.get("x", "0")), float(root.get("y", "0")))
    except (ElementTree.ParseError, KeyError, ValueError) as error:
        raise ValueError("Native SVG has an invalid coordinate frame") from error
    if (
        root.tag != "{http://www.w3.org/2000/svg}svg"
        or view_box != (0.0, 0.0, width, height)
        or size != (width, height)
        or offset != (0.0, 0.0)
        or root.get("transform")
    ):
        raise ValueError("Native SVG frame differs from the physical PDF page")


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def crop_native_svg(
    native_svg: str,
    *,
    width: float,
    height: float,
    bbox: tuple[float, float, float, float],
) -> str:
    """Keep page coordinates and all native content; crop only the viewport."""
    x0, y0, x1, y1 = bbox
    if (
        not all(isfinite(value) for value in bbox)
        or not 0 <= x0 < x1 <= width
        or not 0 <= y0 < y1 <= height
    ):
        raise ValueError("Crop must have finite nonempty bounds inside the page")
    validate_native_svg(native_svg, width=width, height=height)
    view_box = " ".join(_number(value) for value in (x0, y0, x1 - x0, y1 - y0))
    native_root = re.sub(r"^\s*<\?xml[^?]*\?>", "", native_svg, count=1).strip()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_number(x1 - x0)}" height="{_number(y1 - y0)}" '
        f'viewBox="{view_box}" overflow="hidden">'
        f"{native_root}</svg>"
    )
