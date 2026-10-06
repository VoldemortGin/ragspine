"""Which members are running headers / footers, read from the pinned pages' own text.

Recomputed at index time from every selected page's span sidecar, whatever proposed the
page's objects (model layout, the deterministic one of ADR 0028, any later one): a line is
running when ``text_lines.running_lines`` finds its text at the same height on enough pages,
or when it prints a page number inside the top / bottom margin band. A member is running
only when **every** non-blank span it owns lies on such a line — a member that also prints
anything else is left alone.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.deterministic_partition_geometry import (
    FOOTER_BAND_SHARE,
    HEADER_BAND_SHARE,
    is_page_number,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.page.models import ProcessingScope
from ragspine.extraction.evidence.page.text_lines import running_lines, text_lines


@dataclass(frozen=True, slots=True)
class RunningSpans:
    """The ``(page, span id)`` occurrences printed on a running line, and every blank one."""

    running: frozenset[tuple[int, str]]
    blank: frozenset[tuple[int, str]]

    def covers(self, page_index: int, span_ids: Iterable[str]) -> bool:
        """True when the spans print something and all of it is running."""
        printed = [
            (page_index, span_id) for span_id in span_ids if (page_index, span_id) not in self.blank
        ]
        return bool(printed) and all(key in self.running for key in printed)


def running_spans(
    pages: Iterable[tuple[int, float, tuple[TextSpan, ...]]],
) -> RunningSpans:
    """``pages`` is ``(page index, page height, spans)`` for every selected page."""
    pages = tuple(pages)
    keys = running_lines(tuple((height, spans) for _, height, spans in pages))
    running: set[tuple[int, str]] = set()
    blank: set[tuple[int, str]] = set()
    for page_index, height, spans in pages:
        blank.update((page_index, span.span_id) for span in spans if not span.text.strip())
        for line in text_lines(spans):
            bottom = max(span.bbox[3] for span in line.spans)
            margin = bottom <= height * HEADER_BAND_SHARE or line.top >= height * (
                1.0 - FOOTER_BAND_SHARE
            )
            if line.key in keys or (margin and is_page_number(line.text)):
                running.update((page_index, span_id) for span_id in line.span_ids)
    return RunningSpans(frozenset(running), frozenset(blank))


def read_running_spans(sources: LocalDocumentStore, scope: ProcessingScope) -> RunningSpans:
    """``running_spans`` over the scope's selected pages, read from the pinned source."""
    snapshot = sources.load(scope.source_manifest_id)
    return running_spans(
        (
            page_index,
            snapshot.manifest.pages[page_index].height,
            read_text_sidecar(sources, snapshot, page_index).spans,
        )
        for page_index in scope.selected_page_indices
    )
