"""Native SVG shapes of one object crop, in page-top-left coordinates.

Tolerant where ``source_paint._native_paths`` is strict: unknown tags (``<image>``,
``<text>``, ``<clipPath>``) are skipped, not refused, because a diagram proof only needs
the stroked lines and the filled polygons; glyph outlines (non-unit scale transforms) are
skipped too. Nothing here interprets a shape: it only reports what the page draws.
"""

from dataclasses import dataclass
from math import hypot
from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.donut_geometry import (
    _IDENTITY,
    _TOKEN,
    Matrix,
    Point,
    _bounds,
    _compose,
    _matrix,
    _path_controls,
    _polygon,
    _transform,
    _unit_matrix,
)
from enterprise_pdf_rag.documents.models import Bounds
from enterprise_pdf_rag.processing.diagram_models import (
    ARROWHEAD_MAX_AREA,
    NODE_BBOX_TOLERANCE,
    SHAPE_MIN_FILL_RATIO,
    PathEvidence,
)

_SKIPPED = frozenset({"defs", "metadata", "clipPath", "image", "text", "title", "desc"})
_CONTAINERS = frozenset({"svg", "g"})
_COINCIDENT = 1e-9


@dataclass(frozen=True, slots=True)
class NativeShape:
    """One collected ``<path>`` with its composed matrix and inherited paint."""

    path_index: int
    d: str
    matrix: Matrix
    fill: str
    stroke: str
    closed: bool


def _walk(
    node: ElementTree.Element,
    matrix: Matrix,
    fill: str,
    stroke: str,
    shapes: list[NativeShape],
) -> None:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag in _SKIPPED:
        return
    own = node.get("transform")
    if own is not None:
        try:
            matrix = _compose(matrix, _matrix(own))
        except ValueError:
            return
    fill = node.get("fill", fill)
    stroke = node.get("stroke", stroke)
    if tag == "path":
        if _unit_matrix(matrix) and (fill != "none" or stroke != "none"):
            drawing = node.get("d", "")
            shapes.append(NativeShape(len(shapes), drawing, matrix, fill, stroke, "Z" in drawing))
        return
    if tag not in _CONTAINERS:
        return
    for child in node:
        _walk(child, matrix, fill, stroke, shapes)


def native_shapes(svg: str) -> tuple[NativeShape, ...]:
    """Walk the crop, compose every ``transform``, keep paths under a unit-scale matrix."""
    shapes: list[NativeShape] = []
    _walk(ElementTree.fromstring(svg), _IDENTITY, "#000000", "none", shapes)
    return tuple(shapes)


def _area(points: tuple[Point, ...]) -> float:
    return (
        abs(
            sum(
                first[0] * last[1] - last[0] * first[1]
                for first, last in zip(points, points[1:] + points[:1], strict=True)
            )
        )
        / 2
    )


def _distinct(points: tuple[Point, ...]) -> tuple[Point, ...]:
    unique: list[Point] = []
    for point in points:
        if not any(hypot(point[0] - kept[0], point[1] - kept[1]) <= _COINCIDENT for kept in unique):
            unique.append(point)
    return tuple(unique)


def _commands(drawing: str) -> tuple[str, ...]:
    return tuple(token for token in _TOKEN.findall(drawing) if token.isalpha())


def _placed(shape: NativeShape, closed: bool) -> tuple[Point, ...] | None:
    try:
        points = _polygon(shape.d) if closed else _path_controls(shape.d)
    except ValueError:
        return None
    return tuple(_transform(shape.matrix, point) for point in points)


def _covers_object(bounds: Bounds, object_bbox: Bounds) -> bool:
    return (
        bounds[0] < object_bbox[0] - NODE_BBOX_TOLERANCE
        and bounds[1] < object_bbox[1] - NODE_BBOX_TOLERANCE
        and bounds[2] > object_bbox[2] + NODE_BBOX_TOLERANCE
        and bounds[3] > object_bbox[3] + NODE_BBOX_TOLERANCE
    )


def rectangle_like(shape: NativeShape, object_bbox: Bounds) -> PathEvidence | None:
    """A closed filled or stroked frame that fills its own bounding box; never the backdrop."""
    if not shape.closed or (shape.fill == "none" and shape.stroke == "none"):
        return None
    points = _placed(shape, closed=True)
    if points is None:
        return None
    bounds = _bounds(points)
    box = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
    if box <= 0 or _area(points) < SHAPE_MIN_FILL_RATIO * box:
        return None
    if _covers_object(bounds, object_bbox):
        return None
    return PathEvidence(shape.path_index, "shape", points, bounds)


def straight_lines(shape: NativeShape) -> PathEvidence | None:
    """An open stroked polyline of ``M``/``L`` segments only; curved connectors are refused."""
    if shape.stroke == "none" or shape.fill != "none" or shape.closed:
        return None
    commands = _commands(shape.d)
    if not commands or commands[0] != "M" or set(commands) - {"M", "L"} or "L" not in commands:
        return None
    points = _placed(shape, closed=False)
    if points is None or len(points) < 2:
        return None
    return PathEvidence(shape.path_index, "line", points, _bounds(points))


def arrowhead(shape: NativeShape) -> PathEvidence | None:
    """A small closed filled triangle; its direction is derived, never read from the file."""
    if shape.fill == "none" or not shape.closed:
        return None
    points = _placed(shape, closed=True)
    if points is None:
        return None
    vertices = _distinct(points)
    if len(vertices) != 3:
        return None
    bounds = _bounds(vertices)
    if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) > ARROWHEAD_MAX_AREA:
        return None
    return PathEvidence(shape.path_index, "arrowhead", vertices, bounds)


def tip_and_base(head: PathEvidence) -> tuple[Point, Point]:
    """The vertex farthest from the midpoint of the other two, and that midpoint."""
    if len(head.points) != 3:
        raise ValueError("degenerate_arrowhead")
    first, second, third = head.points
    cross = (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )
    if abs(cross) <= _COINCIDENT:
        raise ValueError("degenerate_arrowhead")
    candidates = tuple(
        (point, ((other[0] + last[0]) / 2, (other[1] + last[1]) / 2))
        for point, other, last in (
            (first, second, third),
            (second, third, first),
            (third, first, second),
        )
    )
    return max(candidates, key=lambda pair: hypot(pair[0][0] - pair[1][0], pair[0][1] - pair[1][1]))


def touches(bbox: Bounds, point: Point, *, tolerance: float) -> bool:
    """``point`` lies inside ``bbox`` grown by ``tolerance`` on every edge."""
    return (
        bbox[0] - tolerance <= point[0] <= bbox[2] + tolerance
        and bbox[1] - tolerance <= point[1] <= bbox[3] + tolerance
    )


def segment_crosses(bbox: Bounds, first: Point, last: Point, *, shrink: float) -> bool:
    """Liang-Barsky: does the segment pass through ``bbox`` shrunk by ``shrink``?"""
    left, top = bbox[0] + shrink, bbox[1] + shrink
    right, bottom = bbox[2] - shrink, bbox[3] - shrink
    if left >= right or top >= bottom:
        return False
    dx, dy = last[0] - first[0], last[1] - first[1]
    enter, leave = 0.0, 1.0
    for direction, distance in (
        (-dx, first[0] - left),
        (dx, right - first[0]),
        (-dy, first[1] - top),
        (dy, bottom - first[1]),
    ):
        if direction == 0:
            if distance < 0:
                return False
            continue
        ratio = distance / direction
        if direction < 0:
            enter = max(enter, ratio)
        else:
            leave = min(leave, ratio)
    return enter < leave
