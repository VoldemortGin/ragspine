"""Page text geometry without a model: lines, the headline, running headers (ADR 0025)."""

from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.metadata.deterministic_metadata import deterministic_candidate
from ragspine.extraction.evidence.metadata.page_metadata import PageType, verify_page_metadata
from ragspine.extraction.evidence.metadata.periods import find_periods, period_labels
from ragspine.extraction.evidence.page.text_lines import (
    headline,
    running_lines,
    text_lines,
)

_SHA = "b" * 64


def _span(
    span_id: str, text: str, x: float, top: float, *, size: float = 10.0, width: float = 60.0
) -> TextSpan:
    return TextSpan(span_id, text, (x, top, x + width, top + size), size=size)


_HEADER = _span("h", "Group Interim Report", 20, 10, size=7)
_TITLE_LEFT = _span("t1", "Annual", 20, 40, size=18)
_TITLE_RIGHT = _span("t2", "results 2025", 90, 41, size=18)
_BODY = _span("b", "Revenue rose 5% in FY2024.", 20, 90)
_FOOTNOTE = _span("f", "Source: company filings", 20, 140, size=6)


def test_spans_sharing_a_baseline_band_form_one_line_in_reading_order() -> None:
    lines = text_lines((_BODY, _TITLE_RIGHT, _HEADER, _TITLE_LEFT))

    assert [line.text for line in lines] == [
        "Group Interim Report",
        "Annual results 2025",
        "Revenue rose 5% in FY2024.",
    ]
    assert lines[1].span_ids == ("t1", "t2") and lines[1].size == 18


def test_the_headline_is_the_largest_line_in_the_top_part_never_a_running_line() -> None:
    spans = (_HEADER, _TITLE_LEFT, _TITLE_RIGHT, _BODY, _FOOTNOTE)

    title = headline(spans, page_height=160.0)

    assert title is not None and title.text == "Annual results 2025"
    big_header = _span("h", "Group Interim Report", 20, 10, size=30)
    running = frozenset({("Group Interim Report", 2)})
    overview = _span("o", "Overview", 20, 50)
    found = headline((big_header, overview), page_height=160.0, running=running)
    assert found is not None and found.text == "Overview"
    assert headline((big_header,), page_height=160.0, running=running) is None
    # A page whose only large text sits in its lower half has no headline.
    assert headline((_span("x", "Big but low", 20, 120, size=30),), page_height=160.0) is None


def test_a_running_line_repeats_at_the_same_place_on_enough_pages() -> None:
    pages: list[tuple[float, tuple[TextSpan, ...]]] = [
        (160.0, (_HEADER, _span(f"b{index}", f"Body {index}", 20, 90))) for index in range(10)
    ]
    moved: list[tuple[float, tuple[TextSpan, ...]]] = [
        (160.0, (_span("h", "Group Interim Report", 20, 120, size=7),))
    ]

    running = running_lines(pages + moved)

    assert running == frozenset({("Group Interim Report", 2)})
    # Fewer than 30% of the pages, or a single page, is never a running line.
    rare = [(160.0, (_HEADER,))] * 2 + [
        (160.0, (_span(f"b{index}", f"Body {index}", 20, 90),)) for index in range(8)
    ]
    assert running_lines(rare) == frozenset()
    assert running_lines([(160.0, (_HEADER,))]) == frozenset()


def test_period_labels_keep_the_printed_text_beside_its_canonical_form() -> None:
    text = "Revenue in 1H26 and FY 2024 versus 2023年上半年"

    assert period_labels(text) == (
        ("1H26", "1H2026"),
        ("FY 2024", "FY2024"),
        ("2023年上半年", "1H2023"),
    )
    assert find_periods(text) == ("1H2026", "FY2024", "1H2023")


def test_a_deterministic_candidate_verifies_verbatim_and_leaves_regions_and_type_open() -> None:
    spans = (_HEADER, _TITLE_LEFT, _TITLE_RIGHT, _BODY, _FOOTNOTE)
    running = frozenset({("Group Interim Report", 2)})

    candidate = deterministic_candidate(spans, page_height=160.0, running=running)
    metadata = verify_page_metadata(candidate, spans, source_sha256=_SHA, page_index=0)

    assert metadata.diagnostics == ()
    assert metadata.title is not None and metadata.title.text == "Annual results 2025"
    assert metadata.title.evidence.span_ids == ("t1", "t2")
    assert metadata.section is not None and metadata.section.text == "Group Interim Report"
    # The headline prints a bare year too; every printed period is kept, none inferred.
    assert [period.text for period in metadata.periods] == ["2025", "FY2024"]
    assert metadata.normalized_periods == ("Y2025", "FY2024")
    assert (metadata.page_type, metadata.language, metadata.regions) == (PageType.OTHER, None, ())


def test_a_headline_whose_spans_are_not_consecutive_falls_back_to_its_largest_span() -> None:
    # The sidecar lists the body between the two halves of the title, so the line's text is
    # not a window of consecutive spans; the largest single span still quotes verbatim.
    spans = (_TITLE_LEFT, _BODY, _span("t2", "results", 90, 41, size=20))

    metadata = verify_page_metadata(
        deterministic_candidate(spans, page_height=160.0, running=frozenset()),
        spans,
        source_sha256=_SHA,
        page_index=0,
    )

    assert metadata.title is not None and metadata.title.text == "results"
    assert metadata.section is None and metadata.diagnostics == ()
