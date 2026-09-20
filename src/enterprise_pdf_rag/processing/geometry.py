"""Containment between a model-rendered region and canonical source geometry.

Layout and semantics models receive canonical coordinates in the prompt and echo
them back in their shortest decimal form: pdfspine's ``42.400000000000006`` comes
back as ``42.4`` and ``307.9999999999998`` as ``308``. That rendering noise is
below 1e-12 pt for page-sized values, while the smallest real layout offset is
orders of magnitude above 1e-3 pt, so a fixed 1e-6 pt slack on the outer box
neither unbinds a span from its own region nor admits a span the region misses.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from enterprise_pdf_rag.documents.models import Bounds

COORDINATE_TOLERANCE = 1e-6


def contains(outer: Bounds, inner: Bounds, *, tolerance: float = COORDINATE_TOLERANCE) -> bool:
    """``inner`` is a non-degenerate box lying within ``outer``, up to ``tolerance`` pt per edge.

    ``outer`` is the box a model rendered; ``inner`` keeps its canonical coordinates.
    """
    return (
        outer[0] - tolerance <= inner[0] < inner[2] <= outer[2] + tolerance
        and outer[1] - tolerance <= inner[1] < inner[3] <= outer[3] + tolerance
    )


# Where a ruling sits relative to a grid boundary, sharing the 0.5pt slack that
# ``adapters/pdfspine_tables._contains`` already uses against pdfspine's own float and
# snapping noise. ``COORDINATE_TOLERANCE`` stays reserved for canonical-vs-canonical
# equality; it is far too tight for a painted line's position.
RULING_TOLERANCE = 0.5


class Axis(StrEnum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True, slots=True)
class Segment:
    """One axis-aligned ruling in page-top-left points (the span / ``Table.rows`` frame)."""

    path_index: int
    item_index: int
    edge: str
    axis: Axis
    position: float
    start: float
    end: float
    thickness: float

    def __post_init__(self) -> None:
        values = (self.position, self.start, self.end, self.thickness)
        if (
            self.path_index < 0
            or self.item_index < 0
            or not self.edge
            or not all(isfinite(value) for value in values)
            or self.start >= self.end
            or self.thickness < 0
        ):
            raise ValueError("Ruling segment must be finite, ordered and non-negative")


def coordinate_matches(a: float, b: float, *, tolerance: float = RULING_TOLERANCE) -> bool:
    """Canonical coordinate ``a`` is the same ruling position as ``b`` within ``tolerance`` pt."""
    return abs(a - b) <= tolerance


def rulings_at(
    segments: Sequence[Segment],
    axis: Axis,
    position: float,
    *,
    tolerance: float = RULING_TOLERANCE,
) -> tuple[Segment, ...]:
    """Every ruling along ``axis`` sitting on ``position``, in paint order."""
    return tuple(
        segment
        for segment in segments
        if segment.axis is axis
        and coordinate_matches(segment.position, position, tolerance=tolerance)
    )


def covering_segments(
    segments: Sequence[Segment],
    axis: Axis,
    position: float,
    start: float,
    end: float,
    *,
    tolerance: float = RULING_TOLERANCE,
) -> tuple[Segment, ...] | None:
    """Rulings at ``position`` whose union covers ``[start, end]`` with no gap over ``tolerance``.

    Returns the segments used, in sweep order, or ``None`` when the edge is not
    continuously ruled. Several collinear pieces may be stitched; a segment fully
    inside the already-covered reach is skipped so the result is deterministic.
    """
    if end - start <= tolerance:
        raise ValueError("Edge to cover must be longer than the tolerance")
    candidates = sorted(
        (
            segment
            for segment in rulings_at(segments, axis, position, tolerance=tolerance)
            if segment.end >= start - tolerance and segment.start <= end + tolerance
        ),
        key=lambda segment: (segment.start, segment.end),
    )
    reach = start
    used: list[Segment] = []
    for segment in candidates:
        if segment.start > reach + tolerance:
            return None
        if segment.end > reach:
            used.append(segment)
            reach = segment.end
        if reach >= end - tolerance:
            return tuple(used)
    return None


def segments_crossing(
    segments: Sequence[Segment],
    axis: Axis,
    position: float,
    start: float,
    end: float,
    *,
    tolerance: float = RULING_TOLERANCE,
) -> tuple[Segment, ...]:
    """Rulings at ``position`` running inside the open ``(start, end)`` by more than ``tolerance``."""
    return tuple(
        segment
        for segment in rulings_at(segments, axis, position, tolerance=tolerance)
        if min(segment.end, end - tolerance) - max(segment.start, start + tolerance) > tolerance
    )


def ruling_digest(segments: Sequence[Segment]) -> str:
    """Content digest of the page's rulings in paint order; binds evidence to the drawings."""
    return sha256(
        repr(
            tuple(
                (
                    segment.path_index,
                    segment.item_index,
                    segment.edge,
                    segment.axis.value,
                    segment.position,
                    segment.start,
                    segment.end,
                    segment.thickness,
                )
                for segment in segments
            )
        ).encode()
    ).hexdigest()
