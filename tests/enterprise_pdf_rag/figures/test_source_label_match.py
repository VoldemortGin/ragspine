"""A label is a verbatim run of adjacent source observations, or it is nothing."""

from decimal import Decimal

from enterprise_pdf_rag.figures.models import EvidenceKind, SourceAnchor, SvgElement
from enterprise_pdf_rag.figures.source_label_match import (
    MAX_LABEL_SPANS,
    fold_whitespace,
    match_source_label,
    match_source_value,
    trailing_parenthetical,
    without_trailing_parenthetical,
)

DOCUMENT = "a" * 64


def _element(
    element_id: str,
    text: str,
    bbox: tuple[float, float, float, float],
    *,
    native: bool = False,
) -> SvgElement:
    anchor = SourceAnchor(DOCUMENT, DOCUMENT, 0, bbox, "page-top-left-points")
    if native:
        return SvgElement(element_id, text, anchor)
    return SvgElement(
        element_id,
        text,
        anchor,
        EvidenceKind.SOURCE_TEXT_OBSERVATION,
        f"span-{element_id}",
        (0, len(text)),
    )


def _wrapped() -> tuple[SvgElement, ...]:
    """The real AIA Product-Mix category: one label wrapped over two printed lines."""
    return (
        _element("first", "Traditional", (397.39, 239.25, 454.19, 251.03)),
        _element("second", "Protection", (397.39, 251.85, 449.52, 263.63)),
    )


def _same_line() -> tuple[SvgElement, ...]:
    return (
        _element("first", "Value", (10.0, 10.0, 34.0, 20.0)),
        _element("second", "of", (35.0, 10.0, 45.0, 20.0)),
        _element("third", "New", (46.0, 10.0, 66.0, 20.0)),
        _element("fourth", "Business", (67.0, 10.0, 105.0, 20.0)),
    )


def test_fold_whitespace_collapses_every_run_without_touching_case() -> None:
    assert fold_whitespace("  Traditional \n Protection  ") == "Traditional Protection"
    assert fold_whitespace("   ") == ""


def test_a_wrapped_two_span_label_matches_its_printed_lines() -> None:
    elements = _wrapped()
    ids = tuple(element.element_id for element in elements)

    assert match_source_label(elements, "Traditional Protection", ids) == elements


def test_a_same_line_two_span_label_matches() -> None:
    elements = _same_line()

    assert match_source_label(elements, "Value of", ("first", "second")) == elements[:2]


def test_three_spans_are_the_widest_admissible_label() -> None:
    elements = _same_line()

    assert MAX_LABEL_SPANS == 3
    assert (
        match_source_label(elements, "Value of New", ("first", "second", "third")) == elements[:3]
    )
    assert (
        match_source_label(elements, "Value of New Business", tuple(e.element_id for e in elements))
        is None
    )


def test_non_adjacent_spans_never_concatenate_into_a_label() -> None:
    elements = (
        _element("title", "Distribution Mix", (50.0, 10.0, 170.0, 22.0)),
        _element("category", "Agency", (3.0, 74.0, 40.0, 83.0)),
    )

    assert match_source_label(elements, "Distribution Mix Agency", ("title", "category")) is None


def test_case_differences_are_not_the_same_label() -> None:
    elements = _wrapped()
    ids = tuple(element.element_id for element in elements)

    assert match_source_label(elements, "traditional protection", ids) is None


def test_a_substring_matches_only_the_window_that_prints_it_exactly() -> None:
    elements = _wrapped()
    ids = tuple(element.element_id for element in elements)

    assert match_source_label(elements, "Traditional", ids) == elements[:1]
    assert match_source_label(elements, "Traditional Prot", ids) is None


def test_only_source_text_observations_can_carry_a_label() -> None:
    elements = (
        _element("first", "Traditional", (397.39, 239.25, 454.19, 251.03)),
        _element("second", "Protection", (397.39, 251.85, 449.52, 263.63), native=True),
    )

    assert match_source_label(elements, "Traditional Protection", ("first", "second")) is None
    assert match_source_label(elements, "Protection", ("second",)) is None


def test_an_observation_without_a_source_span_cannot_carry_a_label() -> None:
    elements = _wrapped()
    # The dataclass forbids building this; the matcher still refuses it, because every
    # projected label is receipted as "source-span:<id>:element:<id>".
    object.__setattr__(elements[1], "source_span_id", None)
    ids = tuple(element.element_id for element in elements)

    assert match_source_label(elements, "Traditional Protection", ids) is None


def test_empty_duplicate_or_unknown_citations_fail_closed() -> None:
    elements = _wrapped()

    assert match_source_label(elements, "Traditional", ()) is None
    assert match_source_label(elements, "Traditional", ("first", "first")) is None
    assert match_source_label(elements, "Traditional", ("first", "absent")) is None
    assert match_source_label(elements, "   ", ("first",)) is None


def test_citation_order_does_not_change_the_window() -> None:
    elements = _wrapped()

    assert match_source_label(elements, "Traditional Protection", ("second", "first")) == elements


def test_the_smallest_earliest_window_wins() -> None:
    narrow = (
        _element("first", "Total", (10.0, 10.0, 34.0, 20.0)),
        _element("second", " ", (35.0, 10.0, 38.0, 20.0)),
    )
    assert match_source_label(narrow, "Total", ("first", "second")) == narrow[:1]

    repeated = (
        _element("first", "Mix", (10.0, 10.0, 34.0, 20.0)),
        _element("second", "Mix", (35.0, 10.0, 59.0, 20.0)),
    )
    assert match_source_label(repeated, "Mix", ("first", "second")) == repeated[:1]


def test_a_trailing_parenthetical_is_split_off_or_read_out() -> None:
    assert without_trailing_parenthetical("UFSG per share (US cents)") == "UFSG per share"
    assert trailing_parenthetical("VONB ($m)") == "$m"
    assert trailing_parenthetical("(US cents)") == "US cents"
    assert without_trailing_parenthetical("(US cents)") is None
    assert without_trailing_parenthetical("no parentheses") is None
    assert trailing_parenthetical("unbalanced )") is None
    assert without_trailing_parenthetical("A (b) (c)") == "A (b)"


def test_a_value_printed_with_its_unit_reads_as_one_window() -> None:
    elements = (_element("merged", "8.2%", (100.0, 40.0, 130.0, 52.0)),)

    assert match_source_value(elements, Decimal("8.2"), "%", ("merged",)) == (
        elements,
        Decimal("8.2"),
        True,
    )


def test_a_unit_wrapped_around_the_number_still_reads_as_one_window() -> None:
    elements = (_element("merged", "$965m", (100.0, 40.0, 140.0, 52.0)),)

    assert match_source_value(elements, Decimal("965"), "$m", ("merged",)) == (
        elements,
        Decimal("965"),
        True,
    )


def test_a_bare_number_reads_without_claiming_its_unit() -> None:
    elements = (_element("bare", "937", (100.0, 40.0, 124.0, 52.0)),)

    assert match_source_value(elements, Decimal("937"), "$m", ("bare",)) == (
        elements,
        Decimal("937"),
        False,
    )


def test_accounting_negatives_and_thousands_separators_read_as_printed() -> None:
    negative = (_element("neg", "(130)", (100.0, 40.0, 130.0, 52.0)),)
    grouped = (_element("grouped", "1,234", (100.0, 40.0, 134.0, 52.0)),)

    assert match_source_value(negative, Decimal("-130"), "bps", ("neg",)) == (
        negative,
        Decimal("-130"),
        False,
    )
    assert match_source_value(grouped, Decimal("1234"), "", ("grouped",)) == (
        grouped,
        Decimal("1234"),
        False,
    )


def test_a_window_that_merely_contains_a_number_is_not_that_number() -> None:
    elements = (_element("prose", "up 33% on 1H25", (100.0, 40.0, 190.0, 52.0)),)

    assert match_source_value(elements, Decimal("33"), "%", ("prose",)) is None


def test_a_value_the_figure_does_not_print_never_matches() -> None:
    elements = (_element("printed", "8.2%", (100.0, 40.0, 130.0, 52.0)),)

    assert match_source_value(elements, Decimal("7.4"), "%", ("printed",)) is None
