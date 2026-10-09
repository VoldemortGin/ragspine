"""A chart's lexical text, projected from the PDF text layer inside its rectangle.

A lexical-only chart (``IndexTextOptions.lexical_only_kinds``) is never embedded, so BM25 is
the only channel that can find it. What BM25 scores for it is read here, deterministically,
from the page's own text spans: every non-blank span whose centre lies inside the chart's
rectangle — its title, legend, axis labels and any value label the PDF prints — grouped into
printed lines top to bottom, each line left to right. Nothing is read from a model, nothing
is inferred from bar heights, colours or axis geometry (ADR 0009): a number appears only
when a span prints it, exactly as printed (``1,168`` keeps its separator). Every line keeps
the ids of the spans it was read from, so each word and number has its locator.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from ragspine.extraction.evidence.document.models import Bounds, TextSpan
from ragspine.extraction.evidence.page.text_lines import text_lines


@dataclass(frozen=True, slots=True)
class TextLayerLine:
    """One printed line inside the chart and the spans (on its page) it was read from."""

    text: str
    span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChartTextLayer:
    """The chart's printed lines in reading order; empty when the chart prints no text."""

    page_index: int
    lines: tuple[TextLayerLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def span_ids(self) -> tuple[str, ...]:
        return tuple(span_id for line in self.lines for span_id in line.span_ids)


def _centre_inside(span: TextSpan, bbox: Bounds) -> bool:
    x = (span.bbox[0] + span.bbox[2]) / 2
    y = (span.bbox[1] + span.bbox[3]) / 2
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def chart_text_layer(page_index: int, spans: Iterable[TextSpan], bbox: Bounds) -> ChartTextLayer:
    """The text-layer lines printed inside ``bbox`` (page coordinates, top-left origin)."""
    inside = (span for span in spans if span.text.strip() and _centre_inside(span, bbox))
    return ChartTextLayer(
        page_index,
        tuple(TextLayerLine(line.text, line.span_ids) for line in text_lines(inside)),
    )
