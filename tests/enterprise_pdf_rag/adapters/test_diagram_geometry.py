"""Native SVG shapes of a crop are read geometrically; nothing unknown is trusted."""

import pytest

from enterprise_pdf_rag.adapters.diagram_geometry import (
    arrowhead,
    native_shapes,
    rectangle_like,
    segment_crosses,
    straight_lines,
    tip_and_base,
    touches,
)
from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.processing.diagram_models import PathEvidence, Point

HEIGHT = 160.0
OBJECT_BBOX: Bounds = (15.0, 65.0, 225.0, 105.0)


def crop_svg(*elements: str, bbox: Bounds = OBJECT_BBOX) -> str:
    """The shape ``crop_native_svg`` produces: an outer viewport around the whole page."""
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="{bbox[0]} {bbox[1]} {width} {height}" overflow="hidden">'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="240" height="{HEIGHT}" viewBox="0 0 240 {HEIGHT}">'
        f'<g transform="matrix(1,0,0,-1,0,{HEIGHT})">' + "".join(elements) + "</g></svg></svg>"
    )


def _flip(point: Point) -> str:
    return f"{point[0]} {HEIGHT - point[1]}"


def rect_path(bbox: Bounds, *, fill: str = "none", stroke: str = "#000000") -> str:
    """A stroked or filled rectangle given in page-top-left coordinates."""
    x0, y0, x1, y1 = bbox
    corners = ((x0, y1), (x1, y1), (x1, y0), (x0, y0))
    drawing = "M" + "L".join(_flip(corner) for corner in corners) + "Z"
    return f'<path d="{drawing}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'


def rounded_rect_path(bbox: Bounds, radius: float, *, fill: str = "#cfe3ff") -> str:
    """A cubic-cornered frame like the stacked nodes of the real AIA pathway page."""
    x0, y0, x1, y1 = bbox
    pull = radius * 0.55228
    drawing = (
        "M"
        + _flip((x0 + radius, y0))
        + "L"
        + _flip((x1 - radius, y0))
        + "C"
        + " ".join(
            (
                _flip((x1 - radius + pull, y0)),
                _flip((x1, y0 + radius - pull)),
                _flip((x1, y0 + radius)),
            )
        )
        + "L"
        + _flip((x1, y1 - radius))
        + "C"
        + " ".join(
            (
                _flip((x1, y1 - radius + pull)),
                _flip((x1 - radius + pull, y1)),
                _flip((x1 - radius, y1)),
            )
        )
        + "L"
        + _flip((x0 + radius, y1))
        + "C"
        + " ".join(
            (
                _flip((x0 + radius - pull, y1)),
                _flip((x0, y1 - radius + pull)),
                _flip((x0, y1 - radius)),
            )
        )
        + "L"
        + _flip((x0, y0 + radius))
        + "C"
        + " ".join(
            (
                _flip((x0, y0 + radius - pull)),
                _flip((x0 + radius - pull, y0)),
                _flip((x0 + radius, y0)),
            )
        )
        + "Z"
    )
    return f'<path d="{drawing}" fill="{fill}"/>'


def line_path(*points: Point) -> str:
    """An open stroked polyline given in page-top-left coordinates."""
    drawing = "M" + "L".join(_flip(point) for point in points)
    return f'<path d="{drawing}" fill="none" stroke="#000000" stroke-width="1"/>'


def triangle_path(
    *points: Point, fill: str = "#000000", stroke: str = "none", width: float = 0.5
) -> str:
    """A closed triangle given in page-top-left coordinates; pdfspine emits both twins."""
    drawing = "M" + "L".join(_flip(point) for point in (*points, points[0])) + "Z"
    return (
        f'<path d="{drawing}" fill="{fill}" fill-rule="nonzero" '
        f'stroke="{stroke}" stroke-width="{width}"/>'
    )


def test_native_shapes_compose_page_flip_and_skip_glyph_paths() -> None:
    svg = crop_svg(
        '<defs><clipPath id="clip1"><path d="M0 0L240 0L240 160L0 160Z"/></clipPath></defs>',
        '<image x="0" y="0" width="10" height="10" xlink:href="data:image/png;base64,AA=="/>',
        '<text x="0" y="0" font-size="12">PLAN</text>',
        rect_path((20.0, 70.0, 90.0, 100.0)),
        '<path d="M0.05 0L0.261 0L0.5 0.7L0.05 0.7L0.05 0Z" fill="#000000" '
        'transform="matrix(12,0,0,12,20,120)"/>',
    )

    shapes = native_shapes(svg)

    assert len(shapes) == 1
    frame = rectangle_like(shapes[0], OBJECT_BBOX)
    assert frame is not None
    assert frame.bounds == (20.0, 70.0, 90.0, 100.0)
    assert frame.kind == "shape"


def test_rectangle_like_accepts_rounded_rect_and_rejects_background() -> None:
    node, backdrop = native_shapes(
        crop_svg(
            rounded_rect_path((20.0, 70.0, 120.0, 100.0), 4.8),
            rect_path((0.0, 0.0, 240.0, 160.0), fill="#ffffff", stroke="none"),
        )
    )

    frame = rectangle_like(node, OBJECT_BBOX)
    assert frame is not None
    assert all(
        abs(actual - expected) < 0.01
        for actual, expected in zip(frame.bounds, (20.0, 70.0, 120.0, 100.0), strict=True)
    )
    assert rectangle_like(backdrop, OBJECT_BBOX) is None


def test_arrowhead_tip_and_base() -> None:
    (shape,) = native_shapes(crop_svg(triangle_path((142.0, 81.0), (142.0, 89.0), (150.0, 85.0))))
    head = arrowhead(shape)
    assert head is not None
    assert head.kind == "arrowhead"
    assert set(head.points) == {(142.0, 81.0), (142.0, 89.0), (150.0, 85.0)}
    assert tip_and_base(head) == ((150.0, 85.0), (142.0, 85.0))

    collinear = PathEvidence(0, "arrowhead", ((0.0, 0.0), (1.0, 1.0), (2.0, 2.0)), (0.0,) * 4)
    with pytest.raises(ValueError, match="degenerate_arrowhead"):
        tip_and_base(collinear)


def test_arrowhead_refuses_large_or_unfilled_triangles() -> None:
    big, hollow = native_shapes(
        crop_svg(
            triangle_path((20.0, 20.0), (20.0, 60.0), (60.0, 40.0)),
            triangle_path((142.0, 81.0), (142.0, 89.0), (150.0, 85.0), fill="none", stroke="#000"),
        )
    )
    assert arrowhead(big) is None
    assert arrowhead(hollow) is None


def test_straight_lines_reject_bezier() -> None:
    straight, curved, closed = native_shapes(
        crop_svg(
            line_path((90.0, 85.0), (120.0, 85.0), (142.0, 85.0)),
            '<path d="M90 75C110 95 130 55 142 75" fill="none" stroke="#000000"/>',
            rect_path((20.0, 70.0, 90.0, 100.0)),
        )
    )

    connector = straight_lines(straight)
    assert connector is not None
    assert connector.kind == "line"
    assert connector.points == ((90.0, 85.0), (120.0, 85.0), (142.0, 85.0))
    assert straight_lines(curved) is None
    assert straight_lines(closed) is None


def test_touches_grows_the_box_by_its_tolerance() -> None:
    bbox: Bounds = (150.0, 70.0, 220.0, 100.0)
    assert touches(bbox, (148.0, 85.0), tolerance=2.0)
    assert not touches(bbox, (147.9, 85.0), tolerance=2.0)


def test_segment_crosses_uses_shrunken_box() -> None:
    bbox: Bounds = (100.0, 100.0, 200.0, 200.0)
    assert segment_crosses(bbox, (90.0, 150.0), (210.0, 150.0), shrink=2.0)
    assert not segment_crosses(bbox, (90.0, 101.0), (210.0, 101.0), shrink=2.0)
    assert segment_crosses(bbox, (90.0, 101.0), (210.0, 101.0), shrink=0.0)
    assert not segment_crosses(bbox, (0.0, 0.0), (10.0, 10.0), shrink=2.0)
    assert not segment_crosses(bbox, (90.0, 150.0), (210.0, 150.0), shrink=60.0)
