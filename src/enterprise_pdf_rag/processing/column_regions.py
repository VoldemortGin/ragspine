"""Bind a page's region headings to the column each one stands over.

Page metadata (ADR 0013) is a page-wide fact: every member on a page carries every region
the page names. A slide that prints three country charts side by side therefore hands all
three the same three countries, and no filter can tell the columns apart — the model picks
one and cites it with real provenance, which is a wrong number wearing a correct source.

The geometry already says which heading belongs to which chart: on such a page each heading
sits directly above one column, and the columns do not overlap on the x axis. This module is
that reading and nothing else — no model, no I/O, no page metadata is rewritten. It is
deliberately all-or-nothing: unless every column gets a heading and every heading finds a
column, it returns no binding at all and the caller keeps the page-level values it has always
used. A layout we cannot read must cost nothing, not guess.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

type Bounds = tuple[float, float, float, float]

# One chart on a page needs no column binding; the ambiguity starts at two.
MIN_COLUMNS = 2
# A heading as wide as the page is the page's banner ("ASEAN" over all three countries),
# not a column's name. At or above this share of the page's content width it stays page-wide
# and every member keeps it.
MAX_COLUMN_HEADING_WIDTH_SHARE = 0.5
# Bind a heading to a column only when this much of the heading really sits over it.
MIN_HEADING_OVERLAP_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class PageRegionSpan:
    """One verified page region and the rectangle its evidence was printed in."""

    text: str
    bbox: Bounds | None


@dataclass(frozen=True, slots=True)
class PageColumn:
    """One member competing for the page's headings, by its page rectangle."""

    member_id: str
    bbox: Bounds


@dataclass(frozen=True, slots=True)
class ColumnBinding:
    """What each column is named, and what the whole page is named."""

    page_wide: tuple[str, ...]
    by_member: Mapping[str, tuple[str, ...]]

    def regions_for(self, member_id: str) -> tuple[str, ...]:
        """The page-wide names plus this column's own; empty when nothing was bound."""
        own = self.by_member.get(member_id)
        if own is None:
            return ()
        return self.page_wide + own


EMPTY = ColumnBinding((), {})


def _width(bbox: Bounds) -> float:
    return max(bbox[2] - bbox[0], 0.0)


def _overlap(heading: Bounds, column: Bounds) -> float:
    return max(min(heading[2], column[2]) - max(heading[0], column[0]), 0.0)


def bind_columns(regions: Sequence[PageRegionSpan], columns: Sequence[PageColumn]) -> ColumnBinding:
    """Which of the page's regions names each column, or :data:`EMPTY` when unreadable.

    ``regions`` are the page's verified region values in page order, each with the
    rectangle of the span it was copied from; ``columns`` are the members that may be
    named separately — on a slide of side-by-side charts, the charts. A region with no
    rectangle, or one too wide to be a single column's, is page-wide.
    """
    if len(columns) < MIN_COLUMNS:
        return EMPTY
    content = _content_width(regions, columns)
    if content <= 0:
        return EMPTY
    page_wide: list[str] = []
    headings: list[PageRegionSpan] = []
    for region in regions:
        if region.bbox is None or _width(region.bbox) >= content * MAX_COLUMN_HEADING_WIDTH_SHARE:
            page_wide.append(region.text)
        else:
            headings.append(region)
    if len(headings) < MIN_COLUMNS:
        return EMPTY
    bound: dict[str, list[str]] = {column.member_id: [] for column in columns}
    for heading in headings:
        assert heading.bbox is not None
        share = _width(heading.bbox)
        owners = [
            column
            for column in columns
            if share > 0
            and _overlap(heading.bbox, column.bbox) >= share * MIN_HEADING_OVERLAP_SHARE
        ]
        if not owners:
            # A heading standing over nothing means we are reading the wrong layout.
            return EMPTY
        for column in owners:
            bound[column.member_id].append(heading.text)
    if any(not names for names in bound.values()):
        # A column nobody named would silently lose its region; keep the page-level values.
        return EMPTY
    return ColumnBinding(
        tuple(page_wide), {member: tuple(names) for member, names in bound.items()}
    )


def _content_width(regions: Sequence[PageRegionSpan], columns: Sequence[PageColumn]) -> float:
    boxes = [column.bbox for column in columns]
    boxes.extend(region.bbox for region in regions if region.bbox is not None)
    if not boxes:
        return 0.0
    return max(box[2] for box in boxes) - min(box[0] for box in boxes)
