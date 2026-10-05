"""Page text geometry read without a model: lines, the headline and running lines (ADR 0025).

A *line* is the spans whose vertical extents overlap by at least half the smaller one, read
left to right; its size is its largest span's font size. The *headline* is the largest
line in the top half of the page, never a running line. A *running line* is one text that
recurs at the same height (``RUNNING_BUCKET`` points) on at least ``RUNNING_SHARE`` of the
pages and on two of them at least — a running header or footer. Nothing here reads
meaning; every result is printed text with the spans that printed it.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil

from ragspine.extraction.evidence.document.models import TextSpan

# Two spans share a line when their vertical overlap is at least this share of the shorter.
LINE_OVERLAP = 0.5
# A headline starts in this upper share of the page.
HEADLINE_SHARE = 0.5
# A running line recurs on at least this share of the pages (and at least twice).
RUNNING_SHARE = 0.3
# Running lines match by text and by top edge, bucketed to this many points.
RUNNING_BUCKET = 5.0

type RunningKey = tuple[str, int]


@dataclass(frozen=True, slots=True)
class TextLine:
    """Spans read as one printed line, left to right."""

    spans: tuple[TextSpan, ...]

    @property
    def text(self) -> str:
        return " ".join(" ".join(span.text for span in self.spans).split())

    @property
    def span_ids(self) -> tuple[str, ...]:
        return tuple(span.span_id for span in self.spans)

    @property
    def top(self) -> float:
        return min(span.bbox[1] for span in self.spans)

    @property
    def size(self) -> float:
        return max(span.size for span in self.spans)

    @property
    def key(self) -> RunningKey:
        """What a running line is matched by: its text and its bucketed top edge."""
        return self.text, round(self.top / RUNNING_BUCKET)


def _overlap(first: TextSpan, second: TextSpan) -> bool:
    top = max(first.bbox[1], second.bbox[1])
    bottom = min(first.bbox[3], second.bbox[3])
    shorter = min(first.bbox[3] - first.bbox[1], second.bbox[3] - second.bbox[1])
    return shorter > 0 and bottom - top >= LINE_OVERLAP * shorter


def text_lines(spans: Iterable[TextSpan]) -> tuple[TextLine, ...]:
    """Every printed line of ``spans``, top to bottom; a span joins the first line it overlaps."""
    rows: list[list[TextSpan]] = []
    for span in sorted(
        (span for span in spans if span.text.strip()),
        key=lambda span: (span.bbox[1], span.bbox[0]),
    ):
        row = next((row for row in rows if any(_overlap(span, other) for other in row)), None)
        if row is None:
            rows.append([span])
        else:
            row.append(span)
    lines = (
        TextLine(tuple(sorted(row, key=lambda span: (span.bbox[0], span.bbox[1])))) for row in rows
    )
    return tuple(sorted(lines, key=lambda line: (line.top, line.spans[0].bbox[0])))


def headline(
    spans: Iterable[TextSpan],
    *,
    page_height: float,
    running: frozenset[RunningKey] = frozenset(),
) -> TextLine | None:
    """The largest line starting in the top half of the page, topmost on a tie.

    A running line is never a headline; a page without a sized line there has none.
    """
    candidates = [
        line
        for line in text_lines(spans)
        if line.top < page_height * HEADLINE_SHARE and line.size > 0 and line.key not in running
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: (-line.size, line.top))


def running_lines(
    pages: Sequence[tuple[float, Iterable[TextSpan]]],
) -> frozenset[RunningKey]:
    """The lines recurring at one height on enough pages: ``(text, bucketed top)`` keys.

    ``pages`` is ``(page height, spans)`` per page; a line counts once per page.
    """
    counts: Counter[RunningKey] = Counter()
    for _height, spans in pages:
        counts.update({line.key for line in text_lines(spans)})
    needed = max(2, ceil(RUNNING_SHARE * len(pages)))
    return frozenset(key for key, count in counts.items() if count >= needed)
