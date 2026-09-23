"""Document-level metadata folded deterministically from verified page metadata.

No model runs here. The cover page (``page_type == cover``, else the first selected
page) lends its title; the report period is the normalised period printed on the most
pages; regions form the document's own vocabulary, in order of first appearance. Every
value keeps the page evidence it came from, and a value that no page supports is
``None`` rather than a guess.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ragspine.extraction.evidence.metadata.page_metadata import (
    MetadataValue,
    PageMetadata,
    PageType,
    PeriodValue,
)
from ragspine.extraction.evidence.metadata.periods import period_year

DOCUMENT_METADATA_SCHEMA = "document-metadata-v1"


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    schema_version: str
    display_title: MetadataValue | None
    report_period: PeriodValue | None
    language: str | None
    regions: tuple[MetadataValue, ...]
    years: tuple[int, ...]
    page_count: int
    cover_page_index: int | None


def _mode[T](counts: Counter[T], first_seen: dict[T, int]) -> T | None:
    if not counts:
        return None
    return min(counts, key=lambda key: (-counts[key], first_seen[key]))


def summarize_document(pages: Sequence[PageMetadata]) -> DocumentMetadata | None:
    """Fold verified page metadata into one document record; ``None`` without pages."""
    if not pages:
        return None
    ordered = sorted(pages, key=lambda page: page.page_index)
    cover = next((page for page in ordered if page.page_type is PageType.COVER), ordered[0])
    period_counts: Counter[str] = Counter()
    period_first: dict[str, int] = {}
    period_values: dict[str, PeriodValue] = {}
    language_counts: Counter[str] = Counter()
    language_first: dict[str, int] = {}
    regions: list[MetadataValue] = []
    years: set[int] = set()
    for page in ordered:
        # A page votes once per distinct period it prints.
        for key in page.normalized_periods:
            period_counts[key] += 1
            period_first.setdefault(key, page.page_index)
            period_values.setdefault(
                key, next(period for period in page.periods if period.normalized == key)
            )
            year = period_year(key)
            if year is not None:
                years.add(year)
        if page.language is not None:
            language_counts[page.language] += 1
            language_first.setdefault(page.language, page.page_index)
        for region in page.regions:
            if all(item.text.casefold() != region.text.casefold() for item in regions):
                regions.append(region)
    report_key = _mode(period_counts, period_first)
    report_period = period_values[report_key] if report_key is not None else None
    if report_period is None:
        report_period = next(iter(cover.periods), None)
    return DocumentMetadata(
        DOCUMENT_METADATA_SCHEMA,
        cover.title,
        report_period,
        _mode(language_counts, language_first),
        tuple(regions),
        tuple(sorted(years)),
        len(ordered),
        cover.page_index if cover.page_type is PageType.COVER else None,
    )
