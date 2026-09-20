"""Conservative geometry for a two-sector, directly labelled native donut.

Only absolute M/L/C/Z paths, unit axis reflections/translations, and rectangular
clips are supported. Cubics are flattened to 0.025 point chord tolerance; label
qualification uses a separate one-point boundary margin, never an area estimate.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from math import atan2, hypot, isfinite, pi
from xml.etree import ElementTree

from enterprise_pdf_rag.documents.models import Bounds, TextSpan

type Point = tuple[float, float]
type Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


@dataclass(frozen=True, slots=True)
class NativeSector:
    points: tuple[Point, ...]
    bbox: Bounds
    color: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class NativeDonutGeometry:
    sectors: tuple[NativeSector, ...]
    review_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewedPaintExclusion:
    rule_version: str
    native_svg_digest: str
    path_fingerprint: str
    source_span_fingerprint: str
    crop: Bounds
    paint_bounds: Bounds
    maximum_visible_width: float
    explanation: str

    @property
    def review_id(self) -> str:
        return (
            f"reviewed-source-paint:{self.rule_version}:" + sha256(repr(self).encode()).hexdigest()
        )


NEIGHBOR_GLYPH_REVIEW = ReviewedPaintExclusion(
    "aia-p018-neighbor-u-v1",
    "db2bbd775e003fb5c1e8544ee8c5a31ecf122b9b185158d50fd05792ecc6dd92",
    "c539ed8ce6729514b064f2ac5eb912ca22a47b5e78603052ecd89a697e080713",
    "c1e5bbdd41b0f70c1ef09511da2ac0a0b59bc2c92195874d3d3552ecd7635e63",
    (18.0, 155.0, 250.0, 338.0),
    (249.78032, 218.13904, 255.81008, 225.82672),
    0.21968,
    "Independently reviewed source-specific first U outline of the neighboring Unit-linked span. Only the clipped paint is excluded; no generic glyph mapping or semantic field is qualified.",
)


def _review_exclusion(
    native_digest: str, reference: str, span: TextSpan, bbox: Bounds, hull: Bounds
) -> str:
    review = NEIGHBOR_GLYPH_REVIEW
    if (
        (native_digest, reference, sha256(repr(span).encode()).hexdigest(), bbox)
        != (
            review.native_svg_digest,
            review.path_fingerprint,
            review.source_span_fingerprint,
            review.crop,
        )
        or any(
            abs(actual - expected) > 1e-8
            for actual, expected in zip(hull, review.paint_bounds, strict=True)
        )
        or min(bbox[2], hull[2]) - max(bbox[0], hull[0]) > review.maximum_visible_width + 1e-8
    ):
        raise ValueError("unreviewed_source_paint_at_crop_boundary")
    return review.review_id


def _path_reference(node: ElementTree.Element, matrix: Matrix, clips: tuple[Bounds, ...]) -> str:
    return sha256(
        repr((node.tag, tuple(sorted(node.attrib.items())), matrix, clips)).encode()
    ).hexdigest()


def distance(point: Point, first: Point, last: Point) -> float:
    dx, dy = last[0] - first[0], last[1] - first[1]
    length = dx * dx + dy * dy
    t = (
        0.0
        if length == 0
        else max(
            0.0,
            min(1.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length),
        )
    )
    return hypot(point[0] - first[0] - t * dx, point[1] - first[1] - t * dy)


def contains(points: tuple[Point, ...], point: Point, *, margin: float = 0.0) -> bool:
    crossings = False
    for first, last in zip(points, points[1:] + points[:1], strict=True):
        if distance(point, first, last) <= margin:
            return False
        if (first[1] > point[1]) != (last[1] > point[1]) and point[0] < (last[0] - first[0]) * (
            point[1] - first[1]
        ) / (last[1] - first[1]) + first[0]:
            crossings = not crossings
    return crossings


def _mid(first: Point, last: Point) -> Point:
    return ((first[0] + last[0]) / 2, (first[1] + last[1]) / 2)


def _curve(first: Point, c1: Point, c2: Point, last: Point, depth: int = 0) -> tuple[Point, ...]:
    if max(distance(c1, first, last), distance(c2, first, last)) <= 0.025:
        return (last,)
    if depth >= 16:
        raise ValueError("source_curve_tolerance_unresolved")
    a, b, c = _mid(first, c1), _mid(c1, c2), _mid(c2, last)
    d, e = _mid(a, b), _mid(b, c)
    middle = _mid(d, e)
    return _curve(first, a, d, middle, depth + 1) + _curve(middle, e, c, last, depth + 1)


def _polygon(source: str) -> tuple[Point, ...]:
    if _TOKEN.sub("", source).strip(" ,\t\n\r"):
        raise ValueError("unsupported_source_path")
    tokens = _TOKEN.findall(source)
    points: list[Point] = []
    index = 0
    closed = False
    while index < len(tokens):
        command = tokens[index]
        index += 1
        sizes = {"M": 2, "L": 2, "C": 6, "Z": 0}
        if (
            command not in sizes
            or (command == "M" and points)
            or (command != "M" and not points)
            or closed
        ):
            raise ValueError("unsupported_source_path")
        count = sizes[command]
        if index + count > len(tokens):
            raise ValueError("incomplete_source_path")
        numbers = tuple(float(value) for value in tokens[index : index + count])
        index += count
        if not all(isfinite(value) for value in numbers):
            raise ValueError("nonfinite_source_path")
        if command == "Z":
            closed = True
        elif command in {"M", "L"}:
            points.append((numbers[0], numbers[1]))
        else:
            points.extend(
                _curve(
                    points[-1],
                    (numbers[0], numbers[1]),
                    (numbers[2], numbers[3]),
                    (numbers[4], numbers[5]),
                )
            )
    if not closed or len(points) < 4:
        raise ValueError("source_path_not_closed")
    return tuple(points)


def _transform(matrix: Matrix, point: Point) -> Point:
    a, b, c, d, e, f = matrix
    return (a * point[0] + c * point[1] + e, b * point[0] + d * point[1] + f)


def _matrix(source: str) -> Matrix:
    matched = re.fullmatch(r"matrix\(([^)]+)\)", source)
    if matched is None:
        raise ValueError("unsupported_source_transform")
    values = tuple(float(part) for part in re.split(r"[,\s]+", matched.group(1).strip()))
    if len(values) != 6 or not all(isfinite(value) for value in values):
        raise ValueError("unsupported_source_transform")
    a, b, c, d, e, f = values
    return (a, b, c, d, e, f)


def _unit_matrix(matrix: Matrix) -> bool:
    a, b, c, d, _, _ = matrix
    return abs(a) == 1 and abs(d) == 1 and b == 0 and c == 0


def _path_controls(source: str) -> tuple[Point, ...]:
    """Conservative bounds for locating paint, not for qualifying its geometry."""
    if _TOKEN.sub("", source).strip(" ,\t\r\n"):
        raise ValueError("unsupported_source_path_location")
    tokens = _TOKEN.findall(source)
    index = 0
    current = (0.0, 0.0)
    points: list[Point] = []
    while index < len(tokens):
        command = tokens[index]
        index += 1
        count = {"M": 2, "L": 2, "C": 6, "Q": 4, "H": 1, "V": 1, "Z": 0}.get(command)
        if count is None or index + count > len(tokens):
            raise ValueError("unsupported_source_path_location")
        values = tuple(float(value) for value in tokens[index : index + count])
        index += count
        if not all(isfinite(value) for value in values):
            raise ValueError("nonfinite_source_path")
        if command in {"H", "V"}:
            current = (values[0], current[1]) if command == "H" else (current[0], values[0])
            points.append(current)
        elif values:
            points.extend(zip(values[::2], values[1::2], strict=True))
            current = points[-1]
    if not points:
        raise ValueError("empty_source_path")
    return tuple(points)


def _intersects(first: Bounds, second: Bounds) -> bool:
    return max(first[0], second[0]) < min(first[2], second[2]) and max(first[1], second[1]) < min(
        first[3], second[3]
    )


def _compose(parent: Matrix, child: Matrix) -> Matrix:
    a, b, c, d, e, f = parent
    g, h, i, j, tx, ty = child
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * tx + c * ty + e,
        b * tx + d * ty + f,
    )


def _bounds(points: tuple[Point, ...]) -> Bounds:
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def _inside(inner: Bounds, outer: Bounds) -> bool:
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _annulus(sectors: tuple[NativeSector, ...]) -> None:
    """Prove two complementary circular strips; never infer data from their area."""
    bounds = _bounds(tuple(point for sector in sectors for point in sector.points))
    center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    outer = (bounds[2] - bounds[0] + bounds[3] - bounds[1]) / 4
    radial = tuple(
        hypot(x - center[0], y - center[1]) for sector in sectors for x, y in sector.points
    )
    inner = min(radial)
    tolerance = 0.15
    if (
        abs((bounds[2] - bounds[0]) - (bounds[3] - bounds[1])) > tolerance
        or not 1 < inner < outer - 4
    ):
        raise ValueError("source_annular_topology_unproven")
    seams: list[tuple[tuple[Point, Point], ...]] = []
    sweeps: list[float] = []
    for sector in sectors:
        radii = tuple(hypot(x - center[0], y - center[1]) for x, y in sector.points)
        rings = tuple(0 if abs(radius - inner) < abs(radius - outer) else 1 for radius in radii)
        if any(
            abs(radius - (inner, outer)[ring]) > tolerance
            for radius, ring in zip(radii, rings, strict=True)
        ):
            raise ValueError("source_annular_radii_inconsistent")
        own_seams: list[tuple[Point, Point]] = []
        sweep = [0.0, 0.0]
        indexed = tuple(zip(sector.points, rings, strict=True))
        for (first, ring), (last, next_ring) in zip(
            indexed, indexed[1:] + indexed[:1], strict=True
        ):
            a = (first[0] - center[0], first[1] - center[1])
            b = (last[0] - center[0], last[1] - center[1])
            cross, dot = a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1]
            if ring != next_ring:
                if dot <= 0 or abs(cross) / outer > tolerance:
                    raise ValueError("source_annular_seam_not_radial")
                own_seams.append((first, last) if ring == 1 else (last, first))
            else:
                midpoint = _mid(first, last)
                if (
                    abs(
                        hypot(midpoint[0] - center[0], midpoint[1] - center[1])
                        - (inner, outer)[ring]
                    )
                    > tolerance
                ):
                    raise ValueError("source_annular_arc_not_circular")
                sweep[ring] += atan2(cross, dot)
        if (
            len(own_seams) != 2
            or not 0.01 < abs(sweep[1]) < 2 * pi - 0.01
            or abs(sweep[0] + sweep[1]) > 0.01
        ):
            raise ValueError("source_annular_sector_boundary_incomplete")
        seams.append(tuple(own_seams))
        sweeps.append(sweep[1])
    if sweeps[0] * sweeps[1] <= 0 or abs(abs(sum(sweeps)) - 2 * pi) > 0.01:
        raise ValueError("source_annular_coverage_incomplete")
    for seam in seams[0]:
        if (
            sum(
                all(
                    hypot(a[0] - b[0], a[1] - b[1]) <= tolerance
                    for a, b in zip(seam, other, strict=True)
                )
                for other in seams[1]
            )
            != 1
        ):
            raise ValueError("source_annular_sector_seams_disagree")


def native_donut_geometry(
    svg: str,
    bbox: Bounds,
    native_digest: str,
    *,
    text_spans: tuple[TextSpan, ...],
    excluded_span_ids: tuple[str, ...],
    proven_glyph_refs: tuple[str, ...] = (),
    transparent_refs: tuple[str, ...] = (),
    source_proof_id: str | None = None,
) -> NativeDonutGeometry:
    root = ElementTree.fromstring(svg)
    nodes = {node.get("id"): node for node in root.iter() if node.get("id")}
    sectors: list[NativeSector] = []
    review_refs: list[str] = []
    native = next((child for child in root if child.tag.endswith("}svg")), root)
    page = tuple(float(value) for value in native.attrib["viewBox"].split())
    if len(page) != 4:
        raise ValueError("unsupported_source_page_viewport")
    page_bounds: Bounds = (page[0], page[1], page[0] + page[2], page[1] + page[3])
    paint_index = 0

    def walk(
        node: ElementTree.Element,
        matrix: Matrix,
        clips: tuple[Bounds, ...],
        inherited_fill: str = "#000000",
        unresolved_paint: bool = False,
        inherited_stroke: str = "none",
        inherited_width: str = "1",
    ) -> None:
        nonlocal paint_index
        own = node.get("transform")
        if own:
            matrix = _compose(matrix, _matrix(own))
        fill = node.get("fill", inherited_fill)
        stroke = node.get("stroke", inherited_stroke)
        stroke_width = node.get("stroke-width", inherited_width)
        unresolved_paint = (
            unresolved_paint
            or any(node.get(name) for name in ("mask", "filter", "style", "display", "visibility"))
            or any(
                node.get(name, "1") not in {"1", "1.0"}
                for name in ("opacity", "fill-opacity", "stroke-opacity")
            )
        )
        clip = node.get("clip-path")
        if clip:
            matched = re.fullmatch(r"url\(#([^)]+)\)", clip)
            definition = nodes.get(matched.group(1)) if matched is not None else None
            if (
                definition is None
                or definition.get("transform")
                or definition.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse"
                or len(definition) != 1
            ):
                unresolved_paint = True
            else:
                try:
                    corners = _polygon(definition[0].get("d", ""))
                except ValueError:
                    corners = ()
                if (
                    len(corners) != 4
                    or len({p[0] for p in corners}) != 2
                    or len({p[1] for p in corners}) != 2
                ):
                    unresolved_paint = True
                else:
                    clips = (
                        *clips,
                        _bounds(tuple(_transform(matrix, p) for p in corners)),
                    )
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"svg", "g", "path"}:
            raise ValueError("unsupported_source_paint_primitive")
        if tag == "path" and (fill != "none" or stroke != "none"):
            paint_index += 1
            hull = _bounds(
                tuple(_transform(matrix, point) for point in _path_controls(node.get("d", "")))
            )
            paint_bounds = hull
            if stroke != "none":
                padding = (
                    float(stroke_width)
                    * max(hypot(matrix[0], matrix[1]), hypot(matrix[2], matrix[3]))
                    / 2
                )
                if not isfinite(padding) or padding < 0:
                    raise ValueError("unsupported_source_paint_stroke")
                paint_bounds = (
                    hull[0] - padding,
                    hull[1] - padding,
                    hull[2] + padding,
                    hull[3] + padding,
                )
            if not _intersects(paint_bounds, bbox):
                return
            reference = _path_reference(node, matrix, clips)
            # Only the independent PDF replay/font verifier supplies these
            # references. Publication reconstructs that proof from pinned PDF
            # bytes; a serialized collection of hashes is never authority.
            if source_proof_id is not None and reference in transparent_refs:
                review_refs.append(f"{source_proof_id}:transparent:{reference}")
                return
            if unresolved_paint or stroke != "none":
                raise ValueError("unsupported_source_paint")
            if source_proof_id is not None and reference in proven_glyph_refs:
                review_refs.append(f"{source_proof_id}:glyph:{reference}")
                return
            if paint_index == 1 and fill.lower() == "#ffffff" and _unit_matrix(matrix):
                corners = _polygon(node.get("d", ""))
                if (
                    len(corners) == 4
                    and len({point[0] for point in corners}) == 2
                    and len({point[1] for point in corners}) == 2
                    and hull == page_bounds
                    and (
                        not clips
                        or (
                            source_proof_id is not None
                            and all(_inside(hull, clip_bounds) for clip_bounds in clips)
                        )
                    )
                ):
                    review_refs.append(
                        f"native-first-page-background:{native_digest}:path:{reference}"
                    )
                    return
            if fill.lower() in {"#000000", "#ffffff"}:
                # Font-sized outlines and a matching text bbox do not prove that
                # arbitrary paint is a glyph. Only an exact reviewed source
                # occurrence may be excluded; all other neutral paint blocks
                # qualification until its role is independently established.
                spans = tuple(
                    span
                    for span in text_spans
                    if span.span_id in excluded_span_ids
                    and sha256(repr(span).encode()).hexdigest()
                    == NEIGHBOR_GLYPH_REVIEW.source_span_fingerprint
                )
                if len(spans) != 1 or any(not _inside(hull, clip_bounds) for clip_bounds in clips):
                    raise ValueError("unexplained_source_paint_requires_review")
                review_refs.append(
                    _review_exclusion(native_digest, reference, spans[0], bbox, hull)
                )
                return
            if not _unit_matrix(matrix):
                raise ValueError("unsupported_source_transform")
            if source_proof_id is not None and node.get("fill-rule", "nonzero") != "evenodd":
                raise ValueError("unsupported_source_sector_fill_rule")
            try:
                points = tuple(_transform(matrix, p) for p in _polygon(node.get("d", "")))
            except ValueError:
                raise ValueError("unsupported_source_path") from None
            if points:
                bounds = _bounds(points)
                area = (
                    abs(
                        sum(
                            p[0] * q[1] - q[0] * p[1]
                            for p, q in zip(points, points[1:] + points[:1], strict=True)
                        )
                    )
                    / 2
                )
                if area <= 300 or not _inside(bounds, bbox):
                    raise ValueError("unexplained_source_paint")
                if area > 300:
                    if any(not _inside(bounds, clip_bounds) for clip_bounds in clips):
                        raise ValueError("source_sector_is_clipped")
                    sectors.append(
                        NativeSector(
                            points,
                            bounds,
                            fill,
                            f"native-svg:{native_digest}:path:{reference}",
                        )
                    )
        for child in node:
            if child.tag.rsplit("}", 1)[-1] not in {"defs", "metadata"}:
                walk(child, matrix, clips, fill, unresolved_paint, stroke, stroke_width)

    walk(root, _IDENTITY, ())
    if len(sectors) != 2 or len({sector.color for sector in sectors}) != 2:
        raise ValueError("expected_two_supported_unclipped_native_sectors")
    _annulus(tuple(sectors))
    return NativeDonutGeometry(tuple(sectors), tuple(review_refs))
