"""Question-side filters: periods by rule, regions only from the document's own vocabulary."""

from enterprise_pdf_rag.answers.models import MemberFilters
from enterprise_pdf_rag.answers.query_filters import (
    derive_filters,
    extract_periods,
    extract_years,
    match_regions,
    shared_title_tokens,
    title_matches,
    title_tokens,
)

_VOCABULARY = ("Hong Kong", "Thailand", "Mainland China", "中国内地", "Group")


def test_periods_and_years_come_from_the_shared_rules() -> None:
    assert extract_periods("In the 1H26 Distribution Mix chart, VONB from Agency?") == ("1H2026",)
    assert extract_periods("2026 上半年 分销渠道 占比") == ("1H2026",)
    assert extract_periods("How did 2026 compare with FY2024?") == ("Y2026", "FY2024")
    assert extract_years("How did 2026 compare with FY2024 and 2H26?") == (2026, 2024)
    assert extract_periods("What is VONB?") == () and extract_years("nothing") == ()


def test_regions_match_verbatim_case_insensitively_and_only_from_the_vocabulary() -> None:
    assert match_regions("what about hong kong and THAILAND?", _VOCABULARY) == (
        "Hong Kong",
        "Thailand",
    )
    assert match_regions("中国内地的新业务价值", _VOCABULARY) == ("中国内地",)
    assert match_regions("Singapore margin", _VOCABULARY) == ()  # not in this document
    assert match_regions("tell us about margins", ("US",)) == ()  # short all-caps: exact case
    assert match_regions("US margins", ("US",)) == ("US",)
    assert match_regions("the hk market", ("HK",)) == ()
    assert match_regions("HK VONB", ("HK",)) == ("HK",)
    assert match_regions("Hong Kong", ()) == ()


def test_derive_filters_combines_both_dimensions_or_stays_empty() -> None:
    assert derive_filters("泰国 1H26 VONB", ("Thailand", "泰国")) == MemberFilters(
        ("1H2026",), ("泰国",)
    )
    assert derive_filters("What is VONB?", _VOCABULARY).is_empty


def test_title_match_needs_a_distinctive_word_or_a_cjk_run() -> None:
    title = "AIA Group 2026 Interim Results"
    assert title_matches("AIA 1H26 VONB from Agency", title)
    assert title_matches("what did aia report", title)
    assert not title_matches("interim results overview", title)  # generic words only
    assert not title_matches("what is VONB", title)
    assert title_matches("友邦保险 上半年", "友邦保险控股有限公司 2026 中期业绩")
    assert not title_matches("平安 上半年", "友邦保险控股有限公司 2026 中期业绩")
    assert not title_matches("anything", None)
    # Words two mounted titles share cannot route: only the distinctive ones count.
    titles = ("Meridian 1H26 Hong Kong page 1", "Orion FY2024 Thailand page 1")
    shared = shared_title_tokens(titles)
    assert shared == {"page"}
    assert title_matches("Orion on page 2", titles[1], shared=shared)
    assert not title_matches("Orion on page 2", titles[0], shared=shared)
    assert title_tokens("友邦保险 2026 中期业绩") == {
        "友邦",
        "邦保",
        "保险",
        "中期",
        "期业",
        "业绩",
    }
