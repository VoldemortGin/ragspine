"""Page metadata keeps only verbatim page values; document metadata folds pages without a model."""

from enterprise_pdf_rag.processing.index_text import PageIndexContext, contextual_index_text
from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.metadata.document_metadata import summarize_document
from ragspine.extraction.evidence.metadata.page_metadata import (
    CandidateValue,
    MetadataEvidence,
    MetadataValue,
    PageMetadata,
    PageMetadataCandidate,
    PageType,
    PeriodValue,
    verify_page_metadata,
)

_SHA = "a" * 64


def _span(span_id: str, text: str) -> TextSpan:
    return TextSpan(span_id, text, (0.0, 0.0, 10.0, 10.0))


def _page(
    index: int,
    page_type: PageType,
    *,
    title: str | None = None,
    periods: tuple[tuple[str, str | None], ...] = (),
    regions: tuple[str, ...] = (),
    language: str | None = "en",
) -> PageMetadata:
    evidence = MetadataEvidence(("s1",), "printed")
    return PageMetadata(
        "page-metadata-v1",
        _SHA,
        index,
        page_type,
        language,
        None if title is None else MetadataValue(title, evidence),
        None,
        tuple(PeriodValue(text, evidence, canonical) for text, canonical in periods),
        tuple(MetadataValue(region, evidence) for region in regions),
    )


def test_verification_keeps_verbatim_values_and_drops_the_rest_with_diagnostics() -> None:
    spans = (
        _span("p1-s0", "Distribution   Mix"),
        _span("p1-s1", " 1H26 VONB by channel — Hong Kong and Thailand "),
    )
    candidate = PageMetadataCandidate(
        PageType.CHART,
        "EN",
        CandidateValue("Distribution Mix", "p1-s0"),
        CandidateValue("Business review", "p1-s0"),  # not printed on the page
        (
            CandidateValue("1H26", "p1-s1"),
            CandidateValue("1H26", "p1-s1"),  # duplicate collapses
            CandidateValue("FY2025", "p1-s1"),  # not in the cited span
            CandidateValue("1H26", "p1-s9"),  # unknown span
        ),
        (
            CandidateValue("Hong Kong", "p1-s1"),
            CandidateValue("Thailand", "p1-s1"),
            CandidateValue("  ", "p1-s1"),  # empty
            CandidateValue("Mainland China", "p1-s1"),  # paraphrase, not verbatim
        ),
    )
    metadata = verify_page_metadata(candidate, spans, source_sha256=_SHA, page_index=17)

    assert metadata.schema_version == "page-metadata-v1"
    assert (metadata.source_sha256, metadata.page_index) == (_SHA, 17)
    assert metadata.page_type is PageType.CHART and metadata.language == "en"
    # Evidence text is whitespace-folded so it survives strip-on-validate boundaries.
    assert metadata.title == MetadataValue(
        "Distribution Mix", MetadataEvidence(("p1-s0",), "Distribution Mix")
    )
    assert metadata.section is None
    assert metadata.periods == (
        PeriodValue("1H26", MetadataEvidence(("p1-s1",), spans[1].text.strip()), "1H2026"),
    )
    assert metadata.normalized_periods == ("1H2026",)
    assert tuple(region.text for region in metadata.regions) == ("Hong Kong", "Thailand")
    assert all(region.evidence.span_ids == ("p1-s1",) for region in metadata.regions)
    assert len(metadata.diagnostics) == 5
    assert any(
        "'Business review'" in line and "not verbatim" in line for line in metadata.diagnostics
    )
    assert any("unknown span p1-s9" in line for line in metadata.diagnostics)
    assert any("'Mainland China'" in line for line in metadata.diagnostics)
    assert any("empty value" in line for line in metadata.diagnostics)


def test_a_value_wrapped_over_consecutive_spans_is_verbatim_within_their_window() -> None:
    spans = (
        _span("s0", "2026"),
        _span("s1", "INTERIM RESULTS "),
        _span("s2", "PRESENTATION"),
        _span("s3", "20 AUGUST 2026"),
        _span("s4", "Hong Kong"),
    )
    candidate = PageMetadataCandidate(
        PageType.COVER,
        "en",
        CandidateValue("INTERIM RESULTS PRESENTATION", "s1"),
        CandidateValue("PRESENTATION 20 AUGUST 2026 Hong Kong", "s2"),  # 3 spans: allowed
        (
            CandidateValue("2026", "s0"),
            CandidateValue("2026 INTERIM RESULTS PRESENTATION 20", "s0"),
        ),
        (CandidateValue("RESULTS PRESENTATION", "s2"),),  # starts before the cited span
    )
    metadata = verify_page_metadata(candidate, spans, source_sha256=_SHA, page_index=0)
    assert metadata.title == MetadataValue(
        "INTERIM RESULTS PRESENTATION",
        MetadataEvidence(("s1", "s2"), "INTERIM RESULTS PRESENTATION"),
    )
    assert metadata.section is not None
    assert metadata.section.evidence.span_ids == ("s2", "s3", "s4")
    assert [period.evidence.span_ids for period in metadata.periods] == [
        ("s0",)
    ]  # 4 spans: too wide
    assert metadata.regions == ()
    assert len(metadata.diagnostics) == 2


def test_unnormalisable_period_keeps_its_verbatim_text_only() -> None:
    spans = (_span("s0", "Interim period results"),)
    candidate = PageMetadataCandidate(
        PageType.TEXT, None, None, None, (CandidateValue("Interim period", "s0"),), ()
    )
    metadata = verify_page_metadata(candidate, spans, source_sha256=_SHA, page_index=0)
    assert metadata.periods[0].text == "Interim period"
    assert metadata.periods[0].normalized is None
    assert metadata.normalized_periods == ()
    assert metadata.language is None


def test_document_summary_is_deterministic_and_never_guesses() -> None:
    assert summarize_document(()) is None
    pages = (
        _page(
            2,
            PageType.CHART,
            title="Distribution Mix",
            periods=(("1H26", "1H2026"), ("1H25", "1H2025")),
            regions=("Hong Kong",),
        ),
        _page(
            0,
            PageType.COVER,
            title="ACME 2026 Interim Results",
            periods=(("2026", "Y2026"),),
            language="en",
        ),
        _page(1, PageType.AGENDA, title="Agenda", language="zh"),
        _page(
            3,
            PageType.TEXT,
            periods=(("1H26", "1H2026"), ("1H26", "1H2026")),
            regions=("hong kong", "Thailand"),
        ),
    )
    document = summarize_document(pages)
    assert document is not None
    assert document.schema_version == "document-metadata-v1"
    assert document.display_title is not None
    assert document.display_title.text == "ACME 2026 Interim Results"
    assert document.cover_page_index == 0
    assert document.report_period is not None
    assert document.report_period.normalized == "1H2026"  # two pages vote 1H26; one each otherwise
    assert document.language == "en"
    assert tuple(region.text for region in document.regions) == ("Hong Kong", "Thailand")
    assert document.years == (2025, 2026)
    assert document.page_count == 4
    assert summarize_document(tuple(reversed(pages))) == document


def test_document_without_cover_page_uses_the_first_page_and_falls_back_to_raw_period() -> None:
    pages = (
        _page(4, PageType.TEXT, title="Later page"),
        _page(1, PageType.TEXT, title="First selected page", periods=(("Interim", None),)),
    )
    document = summarize_document(pages)
    assert document is not None
    assert (
        document.display_title is not None and document.display_title.text == "First selected page"
    )
    assert document.cover_page_index is None
    assert document.report_period is not None and document.report_period.normalized is None
    assert document.report_period.text == "Interim"
    assert document.years == () and document.regions == ()


def test_contextual_index_text_prepends_only_present_parts() -> None:
    assert contextual_index_text("body", None) == "body"
    assert contextual_index_text("body", PageIndexContext()) == "body"
    assert (
        contextual_index_text(
            "Distribution Mix 1H26 donut chart figure",
            PageIndexContext("ACME  2026 Interim Results", None, "Business review"),
        )
        == "ACME 2026 Interim Results | Business review\nDistribution Mix 1H26 donut chart figure"
    )
    assert PageIndexContext(None, "Title", "").header() == "Title"
