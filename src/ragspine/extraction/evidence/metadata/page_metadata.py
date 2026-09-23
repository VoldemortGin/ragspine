"""Page-level metadata: what one printed page is about, every value verbatim from that page.

A model proposes a title, section, page type, language, periods and regions for a
page, each citing the source span it was copied from. ``verify_page_metadata`` keeps
a value only when it is a verbatim (whitespace-folded) substring of the cited span's
text; anything else is dropped with a diagnostic, never corrected. Periods are
additionally normalised by ``processing.periods`` — deterministically, or not at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.metadata.periods import normalize_period

PAGE_METADATA_SCHEMA = "page-metadata-v1"


class PageType(StrEnum):
    COVER = "cover"
    AGENDA = "agenda"
    CHART = "chart"
    TABLE = "table"
    TEXT = "text"
    APPENDIX = "appendix"
    OTHER = "other"


# A value may run across a few consecutive lines of the page (a wrapped title): the cited
# span plus at most this many following spans, in page order, form the evidence window.
MAX_EVIDENCE_SPANS = 3


@dataclass(frozen=True, slots=True)
class MetadataEvidence:
    """The consecutive source spans a value was copied from and their (whitespace-folded) text."""

    span_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class MetadataValue:
    text: str
    evidence: MetadataEvidence


@dataclass(frozen=True, slots=True)
class PeriodValue:
    text: str
    evidence: MetadataEvidence
    normalized: str | None


@dataclass(frozen=True, slots=True)
class PageMetadata:
    schema_version: str
    source_sha256: str
    page_index: int
    page_type: PageType
    language: str | None
    title: MetadataValue | None
    section: MetadataValue | None
    periods: tuple[PeriodValue, ...]
    regions: tuple[MetadataValue, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def normalized_periods(self) -> tuple[str, ...]:
        seen: list[str] = []
        for period in self.periods:
            if period.normalized is not None and period.normalized not in seen:
                seen.append(period.normalized)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class CandidateValue:
    """A model-proposed value and the page span id it claims to quote."""

    text: str
    span_id: str


@dataclass(frozen=True, slots=True)
class PageMetadataCandidate:
    page_type: PageType
    language: str | None
    title: CandidateValue | None
    section: CandidateValue | None
    periods: tuple[CandidateValue, ...]
    regions: tuple[CandidateValue, ...]


def fold_whitespace(text: str) -> str:
    return " ".join(text.split())


def _verify(
    field: str, value: CandidateValue, spans: Sequence[tuple[str, str]]
) -> tuple[MetadataValue | None, str | None]:
    folded = fold_whitespace(value.text)
    if not folded:
        return None, f"{field}: empty value dropped"
    start = next(
        (index for index, (span_id, _) in enumerate(spans) if span_id == value.span_id), None
    )
    if start is None:
        return None, f"{field}: {folded!r} cites unknown span {value.span_id}; dropped"
    # The smallest window of consecutive spans, starting at the cited one, that prints the
    # value verbatim. Evidence keeps that text whitespace-folded so it survives every
    # boundary round trip (which strips outer whitespace) byte-identically.
    for width in range(1, MAX_EVIDENCE_SPANS + 1):
        window = spans[start : start + width]
        if len(window) < width:
            break
        text = fold_whitespace(" ".join(span_text for _, span_text in window))
        if folded in text:
            ids = tuple(span_id for span_id, _ in window)
            return MetadataValue(folded, MetadataEvidence(ids, text)), None
    return None, f"{field}: {folded!r} is not verbatim in span {value.span_id}; dropped"


def verify_page_metadata(
    candidate: PageMetadataCandidate,
    page_spans: Sequence[TextSpan],
    *,
    source_sha256: str,
    page_index: int,
) -> PageMetadata:
    """Keep only values that quote this page verbatim; record every drop as a diagnostic.

    A value must be a whitespace-folded substring of its cited span, or of that span
    joined with the next few spans in page order (a title wrapped over several lines).
    """
    spans = tuple((span.span_id, span.text) for span in page_spans)
    diagnostics: list[str] = []

    def single(field: str, value: CandidateValue | None) -> MetadataValue | None:
        if value is None:
            return None
        verified, diagnostic = _verify(field, value, spans)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        return verified

    def many(field: str, values: tuple[CandidateValue, ...]) -> tuple[MetadataValue, ...]:
        kept: list[MetadataValue] = []
        for value in values:
            verified = single(field, value)
            if verified is not None and all(item.text != verified.text for item in kept):
                kept.append(verified)
        return tuple(kept)

    title = single("title", candidate.title)
    section = single("section", candidate.section)
    periods = tuple(
        PeriodValue(value.text, value.evidence, normalize_period(value.text))
        for value in many("periods", candidate.periods)
    )
    regions = many("regions", candidate.regions)
    language = candidate.language.strip().lower() if candidate.language else None
    return PageMetadata(
        PAGE_METADATA_SCHEMA,
        source_sha256,
        page_index,
        candidate.page_type,
        language or None,
        title,
        section,
        periods,
        regions,
        tuple(diagnostics),
    )
