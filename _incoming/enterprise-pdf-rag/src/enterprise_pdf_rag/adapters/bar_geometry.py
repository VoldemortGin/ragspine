"""Match full source labels to upright rectangles without interpreting height.

The caller supplies source-proved fill commands in page coordinates. This
module establishes only the finite direct-label grammar; paint visibility is
an independent prerequisite, never inferred from these bounding boxes.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from math import isfinite

from enterprise_pdf_rag.adapters.donut_geometry import _inside, _intersects
from enterprise_pdf_rag.adapters.source_paint import GlyphPaintProof
from enterprise_pdf_rag.adapters.source_paint_bar import BarVectorPaint
from enterprise_pdf_rag.adapters.stroke_visibility import PathCommand
from enterprise_pdf_rag.documents.models import Bounds, TextSpan

_PERCENT = re.compile(r"[0-9]+(?:\.[0-9]+)?%")
_PERIOD = re.compile(r"[12]H[0-9]{2}")


def _rectangle(commands: tuple[PathCommand, ...]) -> Bounds:
    if not commands or commands[0][0] != "M" or commands[-1] != ("Z", ()):
        raise ValueError("unsupported_bar_rectangle")
    body = commands[:-1]
    if len(body) == 5 and body[-1] == ("L", body[0][1]):
        body = body[:-1]
    if len(body) != 4 or any(
        len(values) != 2 or (index > 0 and operation != "L")
        for index, (operation, values) in enumerate(body)
    ):
        raise ValueError("unsupported_bar_rectangle")
    points = tuple((values[0], values[1]) for _, values in body)
    if not all(isfinite(value) for point in points for value in point):
        raise ValueError("nonfinite_bar_rectangle")
    xs, ys = {point[0] for point in points}, {point[1] for point in points}
    if len(xs) != 2 or len(ys) != 2 or len(set(points)) != 4:
        raise ValueError("unsupported_bar_rectangle")
    for first, second in zip(points, (*points[1:], points[0]), strict=True):
        if first[0] != second[0] and first[1] != second[1]:
            raise ValueError("unsupported_bar_rectangle")
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True, slots=True)
class NativeBarPaint:
    native_path_ref: str
    commands: tuple[PathCommand, ...]

    @property
    def bbox(self) -> Bounds:
        return _rectangle(self.commands)


@dataclass(frozen=True, slots=True)
class DirectBarPoint:
    bar: NativeBarPaint
    category: TextSpan
    literal: TextSpan | None
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class DirectBarGeometry:
    points: tuple[DirectBarPoint, ...]
    rule_version: str = "direct-percent-bar-labels-v1"


@dataclass(frozen=True, slots=True)
class VisibleBarGeometry:
    geometry: DirectBarGeometry
    roles: tuple[tuple[str, str], ...]
    rule_version: str = "visible-direct-percent-bars-v1"


def match_visible_bar_labels(
    *,
    vectors: tuple[BarVectorPaint, ...],
    glyphs: tuple[GlyphPaintProof, ...],
    spans: tuple[TextSpan, ...],
    region: Bounds,
) -> VisibleBarGeometry:
    """Close over every source-proved vector; no paint is ignored by its colour."""
    candidates = []
    for vector in vectors:
        if (
            vector.kind != "fill"
            or vector.alpha != 255
            or not _inside(vector.bounds, region)
        ):
            continue
        try:
            rectangle = _rectangle(vector.commands)
        except ValueError:
            continue
        if rectangle == vector.bounds:
            candidates.append(NativeBarPaint(vector.native_path_ref, vector.commands))
    if len(candidates) < 2:
        raise ValueError("at_least_two_visible_bar_rectangles_required")
    geometry = match_direct_bar_labels(
        bars=tuple(candidates), spans=spans, region=region
    )
    bar_refs = {point.bar.native_path_ref for point in geometry.points}
    roles = []
    for vector in vectors:
        if vector.native_path_ref not in bar_refs:
            role = _auxiliary_role(vector, geometry, vectors, region)
            roles.append((vector.native_path_ref, role))
            continue
        if any(not _inside(vector.bounds, clip) for clip in vector.clips):
            raise ValueError("bar_rectangle_clipped")
        if any(_intersects(glyph.bounds, vector.bounds) for glyph in glyphs):
            raise ValueError("bar_relation_intersects_source_text")
        roles.append((vector.native_path_ref, "bar"))
    return VisibleBarGeometry(geometry, tuple(roles))


def _corridors(geometry: DirectBarGeometry) -> tuple[Bounds, ...]:
    result = []
    for point in geometry.points:
        x0, top, x1, bottom = point.bar.bbox
        category = point.category.bbox
        result.append((max(x0, category[0]), bottom, min(x1, category[2]), category[1]))
        if point.literal is not None:
            literal = point.literal.bbox
            result.append((max(x0, literal[0]), literal[3], min(x1, literal[2]), top))
    return tuple(result)


def _line(
    vector: BarVectorPaint,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if len(vector.commands) != 2:
        return None
    first, second = vector.commands
    if first[0] != "M" or second[0] != "L" or len(first[1]) != 2 or len(second[1]) != 2:
        return None
    return ((first[1][0], first[1][1]), (second[1][0], second[1][1]))


def _convex_polygons(
    commands: tuple[PathCommand, ...],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    result = []
    points: list[tuple[float, float]] = []
    closed = False
    for operation, values in (*commands, ("M", ())):
        if operation == "M":
            if points:
                if points[-1] == points[0]:
                    points.pop()
                    closed = True
                if not closed or len(points) < 3:
                    raise ValueError("unsupported_nonsemantic_arrow")
                turns = []
                for index, point in enumerate(points):
                    second, third = (
                        points[(index + 1) % len(points)],
                        points[(index + 2) % len(points)],
                    )
                    turns.append(
                        (second[0] - point[0]) * (third[1] - second[1])
                        - (second[1] - point[1]) * (third[0] - second[0])
                    )
                if not (
                    all(turn > 0 for turn in turns) or all(turn < 0 for turn in turns)
                ):
                    raise ValueError("unsupported_nonsemantic_arrow")
                result.append(tuple(points))
            points = [(values[0], values[1])] if len(values) == 2 else []
            closed = False
        elif operation == "L" and len(values) == 2 and not closed:
            points.append((values[0], values[1]))
        elif operation == "Z" and not values and points:
            closed = True
        else:
            raise ValueError("unsupported_nonsemantic_arrow")
    return tuple(result)


def _arrow(vector: BarVectorPaint, minimum_width: float) -> bool:
    try:
        shaft, head = _convex_polygons(vector.commands)
    except ValueError:
        return False
    if len(shaft) != 4 or len(head) != 3:
        return False

    def bounds(points: tuple[tuple[float, float], ...]) -> Bounds:
        return (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )

    shaft_box, head_box = bounds(shaft), bounds(head)
    twice_area = abs(
        sum(
            first[0] * second[1] - first[1] * second[0]
            for first, second in zip(shaft, (*shaft[1:], shaft[0]), strict=True)
        )
    )
    return (
        shaft_box[2] > shaft_box[0]
        and twice_area / (2 * (shaft_box[2] - shaft_box[0])) <= minimum_width * 0.08
        and max(head_box[2] - head_box[0], head_box[3] - head_box[1])
        <= minimum_width * 0.5
        and _intersects(shaft_box, head_box)
    )


def _auxiliary_role(
    vector: BarVectorPaint,
    geometry: DirectBarGeometry,
    vectors: tuple[BarVectorPaint, ...],
    region: Bounds,
) -> str:
    if vector.alpha == 0:
        return "transparent"
    if not vector.intersects(region):
        return "outside_region"
    bars = tuple(point.bar.bbox for point in geometry.points)
    minimum_width = min(box[2] - box[0] for box in bars)
    if vector.kind == "fill":
        try:
            background = _rectangle(vector.commands)
        except ValueError:
            background = None
        if (
            background is not None
            and _inside(region, background)
            and vector.paint_order
            < min(
                other.paint_order
                for other in vectors
                if other.native_path_ref
                in {point.bar.native_path_ref for point in geometry.points}
            )
        ):
            return "background_underlay"
        if (
            _arrow(vector, minimum_width)
            and not any(
                vector.intersects(box) for box in (*bars, *_corridors(geometry))
            )
            and all(_inside(vector.bounds, clip) for clip in vector.clips)
        ):
            return "nonsemantic_arrow"
        raise ValueError("unexplained_bar_region_paint")
    if vector.width is None or vector.stroke_ctm is None:
        raise ValueError("stroke_source_parameters_missing")
    from enterprise_pdf_rag.adapters.stroke_visibility import similarity_scale

    effective_width = vector.width * similarity_scale(vector.stroke_ctm)
    if effective_width > minimum_width * 0.05 or any(
        not (
            clip[0] <= vector.bounds[0] <= vector.bounds[2] <= clip[2]
            and clip[1] <= vector.bounds[1] <= vector.bounds[3] <= clip[3]
        )
        for clip in vector.clips
    ):
        raise ValueError("bar_stroke_is_not_a_supported_thin_unclipped_role")
    if any(vector.commands == point.bar.commands for point in geometry.points):
        return "bar_outline"
    endpoints = _line(vector)
    if endpoints is not None:
        first, second = sorted(endpoints)
        baseline = bars[0][3]
        if (
            first[1] == second[1] == baseline
            and first[0] <= bars[0][0]
            and second[0] >= bars[-1][2]
        ):
            return "baseline"
        crossed = tuple(box for box in bars if vector.intersects(box))
        if len(crossed) == 1:
            box = crossed[0]
            if (
                first[0] < box[0] < box[2] < second[0]
                and first[0] < second[0]
                and 0 < abs(second[1] - first[1]) < second[0] - first[0]
                and all(
                    (box[1] + box[3]) / 2 < point[1] < box[3] - effective_width
                    for point in endpoints
                )
                and not any(
                    vector.intersects(corridor) for corridor in _corridors(geometry)
                )
            ):
                return "bar_interruption"
    raise ValueError("unexplained_bar_region_paint")


def _column(span: TextSpan, bounds: Bounds) -> bool:
    left, _, right, _ = bounds
    x0, _, x1, _ = span.bbox
    width = right - left
    center = (x0 + x1) / 2
    overlap = min(x1, right) - max(x0, left)
    return left + width * 0.1 < center < right - width * 0.1 and overlap >= 0.75 * (
        x1 - x0
    )


def _adjacent(span: TextSpan, bounds: Bounds, *, above: bool) -> bool:
    height = span.bbox[3] - span.bbox[1]
    gap = bounds[1] - span.bbox[3] if above else span.bbox[1] - bounds[3]
    return height > 0 and 0 <= gap <= height and _column(span, bounds)


def _near_same_line(first: TextSpan, second: TextSpan) -> bool:
    a, b = first.bbox, second.bbox
    height = min(a[3] - a[1], b[3] - b[1])
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    horizontal_gap = max(a[0], b[0]) - min(a[2], b[2])
    return height > 0 and overlap >= height * 0.5 and horizontal_gap <= height


def match_direct_bar_labels(
    *, bars: tuple[NativeBarPaint, ...], spans: tuple[TextSpan, ...], region: Bounds
) -> DirectBarGeometry:
    """Require one category below each bar; an absent literal stays unavailable."""
    ordered = tuple(sorted(bars, key=lambda bar: bar.bbox[0]))
    if len({bar.bbox[3] for bar in ordered}) != 1:
        raise ValueError("bar_baseline_mismatch")
    if any(first.bbox[2] >= second.bbox[0] for first, second in pairwise(ordered)):
        raise ValueError("bar_columns_overlap")
    points = []
    for bar in ordered:
        bounds = bar.bbox
        if not (
            region[0] <= bounds[0] < bounds[2] <= region[2]
            and region[1] <= bounds[1] < bounds[3] <= region[3]
        ):
            raise ValueError("bar_rectangle_outside_region")
        categories = tuple(
            span
            for span in spans
            if _PERIOD.fullmatch(span.text) and _adjacent(span, bounds, above=False)
        )
        if len(categories) != 1:
            raise ValueError("bar_category_occurrence_ambiguous")
        percentage_displays = tuple(
            span
            for span in spans
            if "%" in span.text and _adjacent(span, bounds, above=True)
        )
        if any(not _PERCENT.fullmatch(span.text) for span in percentage_displays):
            raise ValueError("unsupported_bar_percent_display")
        literals = percentage_displays
        if len(literals) > 1:
            raise ValueError("bar_numeric_occurrence_ambiguous")
        literal = literals[0] if literals else None
        if literal is not None and any(
            span.span_id != literal.span_id
            and span.text.strip()
            and _near_same_line(literal, span)
            for span in spans
        ):
            raise ValueError("bar_numeric_label_has_adjacent_source_text")
        points.append(
            DirectBarPoint(
                bar,
                categories[0],
                literal,
                Decimal(literal.text[:-1]) if literal else None,
            )
        )
    if len({point.category.text for point in points}) != len(points):
        raise ValueError("bar_period_labels_not_unique")
    return DirectBarGeometry(tuple(points))
