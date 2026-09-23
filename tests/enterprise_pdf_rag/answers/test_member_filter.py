"""Candidate narrowing by year / period and region; cover and agenda pages stay out."""

import pytest

from enterprise_pdf_rag.answers.member_filter import (
    candidate_members,
    member_matches,
    region_vocabulary,
)
from enterprise_pdf_rag.answers.models import MemberFilters
from enterprise_pdf_rag.answers.ports import MemberText
from ragspine.extraction.evidence.page.models import ObjectKind


def _member(
    member_id: str,
    *,
    page_type: str | None = "text",
    periods: tuple[str, ...] = (),
    regions: tuple[str, ...] = (),
    member_regions: tuple[str, ...] = (),
) -> MemberText:
    return MemberText(
        member_id,
        ObjectKind.TEXT,
        0,
        "text",
        page_type=page_type,
        periods=periods,
        regions=regions,
        member_regions=member_regions,
    )


_CORPUS = (
    _member("cover", page_type="cover", periods=("Y2026",)),
    _member("agenda", page_type="agenda"),
    _member("hk-1h26", periods=("1H2026", "1H2025"), regions=("Hong Kong",)),
    _member("th-1h26", periods=("1H2026",), regions=("Thailand",)),
    _member("fy24", periods=("FY2024",), regions=("Group",)),
    _member("bare", page_type=None),  # a page without the metadata stage
)


def _allowed(filters: MemberFilters | None) -> set[str] | None:
    allowed = candidate_members(_CORPUS, filters)
    return None if allowed is None else set(allowed)


def test_cover_and_agenda_pages_are_excluded_even_without_filters() -> None:
    assert _allowed(None) == {"hk-1h26", "th-1h26", "fy24", "bare"}
    assert _allowed(MemberFilters()) == {"hk-1h26", "th-1h26", "fy24", "bare"}
    assert candidate_members(_CORPUS[2:], None) is None  # nothing narrows: no restriction


def test_year_filter_matches_every_period_of_the_year_and_finer_filters_match_exactly() -> None:
    assert _allowed(MemberFilters(periods=("2026",))) == {"hk-1h26", "th-1h26"}
    assert _allowed(MemberFilters(periods=("Y2026",))) == {"hk-1h26", "th-1h26"}
    assert _allowed(MemberFilters(periods=("1H25",))) == {"hk-1h26"}
    assert _allowed(MemberFilters(periods=("FY2024",))) == {"fy24"}
    assert _allowed(MemberFilters(periods=("2H26",))) == set()
    assert _allowed(MemberFilters(periods=("Interim",))) == set()  # unknown label never matches


def test_region_filter_is_verbatim_case_insensitive_and_misses_untagged_pages() -> None:
    assert _allowed(MemberFilters(regions=("hong kong",))) == {"hk-1h26"}
    assert _allowed(MemberFilters(regions=("Thailand", "Group"))) == {"th-1h26", "fy24"}
    assert _allowed(MemberFilters(regions=("Mars",))) == set()
    assert _allowed(MemberFilters(periods=("2026",), regions=("Thailand",))) == {"th-1h26"}
    assert not member_matches(_CORPUS[-1], MemberFilters(regions=("Hong Kong",)))


def test_region_filter_matches_whole_words_so_a_page_may_qualify_the_place_further() -> None:
    thailand = MemberFilters(regions=("Thailand",))
    assert member_matches(_member("aia-th", regions=("AIA Thailand",)), thailand)
    assert member_matches(_member("th", regions=("Thailand",)), thailand)
    assert member_matches(
        _member("hk-sar", regions=("Hong Kong Special Administrative Region",)),
        MemberFilters(regions=("Hong Kong",)),
    )
    assert member_matches(
        _member("tw", regions=("Taiwan (China)",)), MemberFilters(regions=("Taiwan",))
    )
    assert not member_matches(
        _member("aia-th", regions=("AIA Thailand",)), MemberFilters(regions=("Singapore",))
    )
    # Every word of the filter must appear: a narrower filter is not met by a broader value.
    assert not member_matches(
        _member("th", regions=("Thailand",)), MemberFilters(regions=("AIA Thailand",))
    )


# One page of three country charts side by side: the banner names the page, each heading
# names one column, and every member of the page carries all four (ADR 0013).
_COLUMN_PAGE = ("ASEAN", "AIA Thailand", "AIA Singapore", "AIA Malaysia")


def test_a_member_the_page_geometry_named_answers_on_its_own_regions_alone() -> None:
    """The chart under the Thailand heading is Thailand, though its page names three markets.

    Without this the model picks one of the three columns and cites it with real provenance,
    which is a wrong number wearing a correct source.
    """
    chart = _member("th-chart", regions=_COLUMN_PAGE, member_regions=("ASEAN", "AIA Thailand"))

    assert member_matches(chart, MemberFilters(regions=("Thailand",)))
    assert member_matches(chart, MemberFilters(regions=("ASEAN",)))  # the page-wide banner
    # Its neighbours' headings are still on the page and no longer on this member.
    assert not member_matches(chart, MemberFilters(regions=("Singapore",)))
    assert not member_matches(chart, MemberFilters(regions=("AIA Malaysia",)))


def test_a_member_the_geometry_could_not_name_still_answers_for_its_whole_page() -> None:
    """Empty member regions means the page does not read as columns, not that it has none."""
    paragraph = _member("body", regions=_COLUMN_PAGE)

    assert paragraph.member_regions == ()
    for wanted in _COLUMN_PAGE:
        assert member_matches(paragraph, MemberFilters(regions=(wanted,))), wanted
    assert not member_matches(paragraph, MemberFilters(regions=("Vietnam",)))


def test_naming_one_column_does_not_shrink_the_document_region_vocabulary() -> None:
    """A question is parsed against what the pages print, not against what each member kept."""
    corpus = (
        _member("th", regions=_COLUMN_PAGE, member_regions=("ASEAN", "AIA Thailand")),
        _member("sg", regions=_COLUMN_PAGE, member_regions=("ASEAN", "AIA Singapore")),
        _member("my", regions=_COLUMN_PAGE, member_regions=("ASEAN", "AIA Malaysia")),
    )

    assert region_vocabulary(corpus) == _COLUMN_PAGE


def test_a_region_that_excludes_a_place_never_matches_it() -> None:
    thailand = MemberFilters(regions=("Thailand",))
    assert not member_matches(_member("ex-th", regions=("ex-Thailand",)), thailand)
    assert not member_matches(_member("excl", regions=("Group excluding Thailand",)), thailand)
    assert not member_matches(
        _member("ex-jp", regions=("Asia ex-Japan",)), MemberFilters(regions=("Japan",))
    )
    assert not member_matches(
        _member("non-hk", regions=("non-Hong Kong",)), MemberFilters(regions=("Hong Kong",))
    )
    assert member_matches(  # no false positive from the "ex" test
        _member("sg", regions=("Singapore",)), MemberFilters(regions=("Singapore",))
    )


def test_vocabulary_and_filter_validation() -> None:
    assert region_vocabulary(_CORPUS) == ("Hong Kong", "Thailand", "Group")
    assert region_vocabulary((_member("x", regions=("HONG KONG",)), *_CORPUS)) == (
        "HONG KONG",
        "Thailand",
        "Group",
    )
    with pytest.raises(ValueError, match="nonempty"):
        MemberFilters(periods=(" ",))
