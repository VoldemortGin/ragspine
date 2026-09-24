"""页标签（页图按需附图的触发依据）：从 DiPage 的块算出原始度量，再按阈值得出标签。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.page_tags import (
    DEFAULT_FIGURE_MIN_CHARS,
    DEFAULT_LOW_TEXT_CHARS,
    PAGE_TAG_NAMES,
    PageTagStats,
    document_tag_stats,
    page_tag_stats,
    page_tags,
)
from ragspine.extraction.di_markdown.parse import parse_di_markdown

_LONG = "Operating profit grew strongly across every reporting segment this half. " * 6


def _page(md: str):
    return parse_di_markdown(md).pages[0]


def test_defaults_and_names():
    assert (DEFAULT_LOW_TEXT_CHARS, DEFAULT_FIGURE_MIN_CHARS) == (300, 10)
    assert PAGE_TAG_NAMES == ("has_table", "has_figure", "low_text")


def test_html_table_page():
    md = "# Results\n\n<table><tr><th>Year</th><th>VONB</th></tr><tr><td>1H26</td><td>3,212</td></tr></table>\n"
    stats = page_tag_stats(_page(md))
    assert stats.has_table
    assert stats.n_figures == 0
    # 标题 + 单元格文字都计入，去掉空白
    assert stats.text_chars == len("Results") + len("YearVONB1H263,212")
    assert page_tags(stats) == frozenset({"has_table", "low_text"})


def test_pipe_table_fallback_counts_as_table_and_strips_pipes():
    md = "| Year | VONB |\n| --- | ---: |\n| 1H26 | 3,212 |\n"
    stats = page_tag_stats(_page(md))
    assert stats.has_table
    # 去掉空白与 `|` 后计字符（分隔行的 - 与 : 照计，口径同 SuperIndex）
    assert stats.text_chars == len("YearVONB" + "---" + "---:" + "1H263,212")


def test_horizontal_rule_is_not_a_table():
    stats = page_tag_stats(_page(f"{_LONG}\n\n---\n\n{_LONG}\n"))
    assert not stats.has_table


def test_figure_measures_text_and_caption():
    md = (
        "<figure>\n<figcaption>VONB growth</figcaption>\n+17%\nCAGR\n</figure>\n\n"
        "<figure>\nAIA\n</figure>\n"
    )
    stats = page_tag_stats(_page(md))
    assert stats.n_figures == 2
    assert stats.figure_max_chars == len("VONBgrowth+17%CAGR")
    assert "has_figure" in page_tags(stats)


def test_small_figure_is_ignored_by_threshold():
    stats = page_tag_stats(_page(f"{_LONG}\n\n<figure>\nAIA\n</figure>\n"))
    assert (stats.n_figures, stats.figure_max_chars) == (1, 3)
    assert page_tags(stats) == frozenset()
    assert page_tags(stats, figure_min_chars=3) == frozenset({"has_figure"})
    assert page_tags(stats, figure_min_chars=0) == frozenset({"has_figure"})


def test_empty_figure_counts_only_with_zero_threshold():
    stats = page_tag_stats(_page(f"{_LONG}\n\n<figure>\n</figure>\n"))
    assert (stats.n_figures, stats.figure_max_chars) == (1, 0)
    assert "has_figure" not in page_tags(stats)
    assert "has_figure" in page_tags(stats, figure_min_chars=0)


def test_low_text_threshold_is_configurable():
    stats = page_tag_stats(_page(_LONG))
    assert stats.text_chars >= 300
    assert "low_text" not in page_tags(stats)
    assert "low_text" in page_tags(stats, low_text_chars=stats.text_chars + 1)
    assert "low_text" not in page_tags(stats, low_text_chars=stats.text_chars)


def test_comments_headers_and_footers_do_not_count():
    md = (
        '<!-- PageHeader="AIA Group Limited" -->\n'
        "Short body.\n"
        "<!-- some hidden note with lots of words that should never count as text -->\n"
        '<!-- PageFooter="Confidential - for internal use only" -->\n'
        '<!-- PageNumber="12" -->\n'
    )
    stats = page_tag_stats(_page(md))
    assert stats.text_chars == len("Shortbody.")


def test_empty_page():
    stats = page_tag_stats(_page(""))
    assert stats == PageTagStats(
        page=1, has_table=False, n_figures=0, figure_max_chars=0, text_chars=0
    )
    assert page_tags(stats) == frozenset({"low_text"})


def test_document_stats_use_physical_page_order():
    md = f'{_LONG}\n<!-- PageNumber="7" -->\n<!-- PageBreak -->\n<table><tr><td>x</td></tr></table>\n'
    stats = document_tag_stats(parse_di_markdown(md))
    assert [s.page for s in stats] == [1, 2]
    assert [s.has_table for s in stats] == [False, True]


@pytest.mark.parametrize("bad", [-1])
def test_negative_thresholds_rejected(bad):
    stats = page_tag_stats(_page(_LONG))
    with pytest.raises(ValueError):
        page_tags(stats, low_text_chars=bad)
    with pytest.raises(ValueError):
        page_tags(stats, figure_min_chars=bad)
