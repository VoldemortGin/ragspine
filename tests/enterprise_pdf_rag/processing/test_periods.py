"""Period labels normalise deterministically or not at all; a year matches its periods."""

import pytest

from ragspine.extraction.evidence.metadata.periods import (
    find_periods,
    normalize_period,
    period_matches,
    period_year,
)


@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        ("1H26", "1H2026"),
        ("1H 2026", "1H2026"),
        ("H1'26", "1H2026"),
        ("2H FY26", "2H2026"),
        ("2026年上半年", "1H2026"),
        ("2026 下半年", "2H2026"),
        ("2026H1", "1H2026"),
        ("FY24", "FY2024"),
        ("FY 2024", "FY2024"),
        ("2024财年", "FY2024"),
        ("Q1 2025", "Q1-2025"),
        ("1Q25", "Q1-2025"),
        ("2025Q3", "Q3-2025"),
        ("2025年第一季度", "Q1-2025"),
        ("2026", "Y2026"),
        ("2026年", "Y2026"),
    ],
)
def test_known_labels_normalise(label: str, canonical: str) -> None:
    assert normalize_period(label) == canonical


@pytest.mark.parametrize("label", ["上半年", "Interim 2026", "1H26 Distribution Mix", "26", "H1"])
def test_unknown_labels_are_not_guessed(label: str) -> None:
    assert normalize_period(label) is None


def test_find_periods_scans_free_text_without_splitting_words() -> None:
    assert find_periods("In the 1H26 Distribution Mix chart, VONB from Agency?") == ("1H2026",)
    assert find_periods("2026 上半年 分销渠道 占比") == ("1H2026",)
    assert find_periods("泰国 1H26 VONB 与 FY2024 对比 2H25年") == ("1H2026", "FY2024", "2H2025")
    assert find_periods("what is 2026 revenue") == ("Y2026",)
    assert find_periods("A1H26 and 2026x are not periods") == ()
    assert find_periods("AIA 2026 Interim Results 1H26") == ("Y2026", "1H2026")


def test_year_query_matches_every_period_of_that_year_only() -> None:
    assert period_year("Q1-2025") == 2025 and period_year("junk") is None
    assert period_matches("Y2026", "1H2026")
    assert period_matches("Y2026", "FY2026")
    assert period_matches("1H2026", "1H2026")
    assert not period_matches("1H2026", "Y2026")
    assert not period_matches("FY2026", "1H2026")
    assert not period_matches("Y2025", "1H2026")
    assert not period_matches("junk", "1H2026")
