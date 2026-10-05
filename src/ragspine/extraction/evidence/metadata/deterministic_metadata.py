"""Page metadata candidates derived from the page's own text geometry, no model (ADR 0025).

What the model would have been asked for, read deterministically instead:

- **title** — the page's headline (``page.text_lines.headline``: the largest line in the top
  half, never a running line), cited as its whole line when its spans are consecutive in page
  order (at most ``MAX_EVIDENCE_SPANS``), else its largest single span;
- **section** — the first running line in the top half of the page (a running header);
- **periods** — every label ``periods.period_labels`` recognises, cited to the span printing it;
- **regions** — none: ADR 0013 refuses a gazetteer, and region words are vocabulary only a
  reader (or a model) assigns;
- **page type** — ``other``, **language** — none: neither is printed, so neither is guessed.

The candidate goes through ``verify_page_metadata`` exactly like a model's, so every kept
value still quotes its page verbatim.
"""

from collections.abc import Sequence

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.metadata.page_metadata import (
    MAX_EVIDENCE_SPANS,
    CandidateValue,
    PageMetadataCandidate,
    PageType,
)
from ragspine.extraction.evidence.metadata.periods import period_labels
from ragspine.extraction.evidence.page.text_lines import (
    HEADLINE_SHARE,
    RunningKey,
    TextLine,
    headline,
    text_lines,
)


def _cited(line: TextLine, order: dict[str, int]) -> CandidateValue:
    """The whole line when its spans are a window of the page order, else its largest span."""
    positions = sorted(order[span_id] for span_id in line.span_ids)
    consecutive = positions == list(range(positions[0], positions[0] + len(positions)))
    if consecutive and len(positions) <= MAX_EVIDENCE_SPANS:
        first = min(line.spans, key=lambda span: order[span.span_id])
        ordered = sorted(line.spans, key=lambda span: order[span.span_id])
        return CandidateValue(" ".join(span.text for span in ordered), first.span_id)
    largest = max(line.spans, key=lambda span: (span.size, -order[span.span_id]))
    return CandidateValue(largest.text, largest.span_id)


def deterministic_candidate(
    spans: Sequence[TextSpan],
    *,
    page_height: float,
    running: frozenset[RunningKey],
) -> PageMetadataCandidate:
    """A page's metadata candidate from its spans and the document's running lines."""
    order = {span.span_id: index for index, span in enumerate(spans)}
    title = headline(spans, page_height=page_height, running=running)
    section = next(
        (
            line
            for line in text_lines(spans)
            if line.key in running and line.top < page_height * HEADLINE_SHARE
        ),
        None,
    )
    periods = tuple(
        CandidateValue(label, span.span_id)
        for span in spans
        for label, _canonical in period_labels(span.text)
    )
    return PageMetadataCandidate(
        PageType.OTHER,
        None,
        None if title is None else _cited(title, order),
        None if section is None else _cited(section, order),
        periods,
        (),
    )
