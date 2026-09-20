"""Bound all legal PDF cap/join choices without asserting their unknown values.

PDF Reference 1.7, section 4.3.2: the stroke body lies within half a line
width of the path. A join's outer miter tip is h/sin(interior_angle/2)
from its vertex. Every miter limit either retains that tip or clips to a
bevel. Bounds here are conservative visibility evidence, never SVG fidelity.
"""

from dataclasses import dataclass
from itertools import pairwise
from math import hypot, isfinite, sqrt
from typing import Literal

from enterprise_pdf_rag.documents.models import Bounds

type Point = tuple[float, float]
type PathCommand = tuple[str, tuple[float, ...]]
type Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _touches(first: Bounds, second: Bounds) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _box(points: tuple[Point, ...], padding: float) -> Bounds:
    bounds = (
        min(point[0] for point in points) - padding,
        min(point[1] for point in points) - padding,
        max(point[0] for point in points) + padding,
        max(point[1] for point in points) + padding,
    )
    if not all(isfinite(value) for value in bounds):
        raise ValueError("nonfinite_stroke_envelope")
    return bounds


def _unit(vector: Point) -> Point:
    length = hypot(*vector)
    if not isfinite(length) or length <= 1e-9:
        raise ValueError("degenerate_stroke_tangent")
    return (vector[0] / length, vector[1] / length)


def similarity_scale(matrix: Matrix) -> float:
    """Reject shear, anisotropic scale and degenerate transforms, without guessing."""
    a, b, c, d, _, _ = matrix
    if (
        not all(isfinite(value) for value in matrix)
        or a * a + b * b != c * c + d * d
        or a * c + b * d != 0
    ):
        raise ValueError("unsupported_stroke_transform")
    scale = hypot(a, b)
    if not isfinite(scale) or scale <= 0:
        raise ValueError("unsupported_stroke_transform")
    return scale


@dataclass(frozen=True, slots=True)
class StrokeEnvelope:
    components: tuple[Bounds, ...]
    style_status: Literal["bounded_unknown_style"] = "bounded_unknown_style"
    rule_version: str = "pdf-solid-stroke-envelope-v1"

    def intersects(self, bounds: Bounds) -> bool:
        return any(_touches(component, bounds) for component in self.components)


@dataclass(frozen=True, slots=True)
class _Segment:
    points: tuple[Point, ...]

    @property
    def incoming(self) -> Point:
        return _unit(
            (
                self.points[-1][0] - self.points[-2][0],
                self.points[-1][1] - self.points[-2][1],
            )
        )

    @property
    def outgoing(self) -> Point:
        return _unit(
            (
                self.points[1][0] - self.points[0][0],
                self.points[1][1] - self.points[0][1],
            )
        )

    @property
    def curved(self) -> bool:
        return len(self.points) == 4


def _regular_curve(points: tuple[Point, ...]) -> None:
    # The derivative is a convex combination of the three control differences.
    # A common strictly positive projection therefore proves it never vanishes.
    direction = _unit((points[-1][0] - points[0][0], points[-1][1] - points[0][1]))
    for start, end in pairwise(points):
        difference = (end[0] - start[0], end[1] - start[1])
        if difference[0] * direction[0] + difference[1] * direction[1] <= 1e-9:
            raise ValueError("unproved_or_degenerate_stroke_curve")


def _segments(commands: tuple[PathCommand, ...]) -> tuple[tuple[_Segment, ...], bool]:
    if not commands or commands[0][0] != "M" or len(commands[0][1]) != 2:
        raise ValueError("unsupported_stroke_path")
    if not all(isfinite(value) for _, values in commands for value in values):
        raise ValueError("nonfinite_stroke_path")
    start: Point = (commands[0][1][0], commands[0][1][1])
    current = start
    result: list[_Segment] = []
    closed = False
    for index, (operation, values) in enumerate(commands[1:], 1):
        if operation == "Z" and not values and index == len(commands) - 1:
            closed = True
            if current != start:
                result.append(_Segment((current, start)))
        elif operation == "L" and len(values) == 2:
            target = (values[0], values[1])
            _unit((target[0] - current[0], target[1] - current[1]))
            result.append(_Segment((current, target)))
            current = target
        elif operation == "C" and len(values) == 6:
            points = (
                current,
                (values[0], values[1]),
                (values[2], values[3]),
                (values[4], values[5]),
            )
            _regular_curve(points)
            result.append(_Segment(points))
            current = points[-1]
        else:
            raise ValueError("unsupported_stroke_path")
    if not result:
        raise ValueError("degenerate_stroke_path")
    return tuple(result), closed


def _join(first: _Segment, second: _Segment, half_width: float) -> Bounds:
    incoming, outgoing = first.incoming, second.outgoing
    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
    # sin(interior_angle/2) == sqrt((1 + cos(turn_angle))/2).
    squared = (1.0 + max(-1.0, min(1.0, dot))) / 2.0
    if squared <= 1e-12:
        raise ValueError("unbounded_or_near_reverse_stroke_join")
    extent = half_width / sqrt(squared)
    return _box((first.points[-1],), extent)


def stroke_envelope(
    commands: tuple[PathCommand, ...],
    *,
    width: float,
    stroke_ctm: Matrix = _IDENTITY,
    dashes: str = "",
    region: Bounds | None = None,
) -> StrokeEnvelope:
    """Return per-segment coverage, preserving empty interiors of closed paths."""
    if not isfinite(width) or width <= 0:
        raise ValueError("positive_finite_stroke_width_required")
    if dashes:
        raise ValueError("unsupported_stroke_dash")
    if region is not None and (
        not all(isfinite(value) for value in region)
        or region[0] >= region[2]
        or region[1] >= region[3]
    ):
        raise ValueError("invalid_stroke_region")
    segments, closed = _segments(commands)
    half_width = width * similarity_scale(stroke_ctm) / 2.0
    if not isfinite(half_width) or half_width <= 0:
        raise ValueError("positive_finite_stroke_width_required")
    components = [_box(segment.points, half_width) for segment in segments]
    curve_components = [
        component for segment, component in zip(segments, components, strict=True) if segment.curved
    ]
    joins = list(pairwise(segments))
    if closed:
        joins.append((segments[-1], segments[0]))
    else:
        # A projecting square cap's farthest corner is sqrt(2)*h away.
        radius = sqrt(2.0) * half_width
        components.extend(
            (
                _box((segments[0].points[0],), radius),
                _box((segments[-1].points[-1],), radius),
            )
        )
        if segments[0].curved:
            curve_components.append(components[-2])
        if segments[-1].curved:
            curve_components.append(components[-1])
    for first, second in joins:
        join = _join(first, second, half_width)
        components.append(join)
        if first.curved or second.curved:
            curve_components.append(join)
    if curve_components and (
        region is None or any(_touches(box, region) for box in curve_components)
    ):
        raise ValueError("stroke_curve_not_proved_outside_region")
    return StrokeEnvelope(tuple(components))
