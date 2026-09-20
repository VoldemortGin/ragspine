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

from enterprise_pdf_rag.documents.models import TextSpan
from enterprise_pdf_rag.processing.periods import normalize_period

PAGE_METADATA_SCHEMA = "page-metadata-v1"


class PageType(StrEnum):
    COVER = "cover"
    AGENDA = "agenda"
    CHART = "chart"
    TABLE = "table"
    TEXT = "text"
    APPENDIX = "appendix"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class MetadataEvidence:
    """The source span a value was copied from: its id and its full printed text."""

    span_id: str
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
    field: str, value: CandidateValue, spans: dict[str, str]
) -> tuple[MetadataValue | None, str | None]:
    folded = fold_whitespace(value.text)
    if not folded:
        return None, f"{field}: empty value dropped"
    span_text = spans.get(value.span_id)
    if span_text is None:
        return None, f"{field}: {folded!r} cites unknown span {value.span_id}; dropped"
    if folded not in fold_whitespace(span_text):
        return None, f"{field}: {folded!r} is not verbatim in span {value.span_id}; dropped"
    return MetadataValue(folded, MetadataEvidence(value.span_id, span_text)), None


def verify_page_metadata(
    candidate: PageMetadataCandidate,
    page_spans: Sequence[TextSpan],
    *,
    source_sha256: str,
    page_index: int,
) -> PageMetadata:
    """Keep only values that quote this page verbatim; record every drop as a diagnostic."""
    spans = {span.span_id: span.text for span in page_spans}
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
