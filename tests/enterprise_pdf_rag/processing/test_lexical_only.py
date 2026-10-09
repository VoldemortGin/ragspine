"""Lexical-only kinds (pure): the index version names them, and a chart's lexical text is
projected from the PDF text layer inside its rectangle — every word and number a span's own."""

import re

import pytest

from enterprise_pdf_rag.processing.index_text import (
    INDEX_VERSION,
    ONE_UNIT_EACH,
    UNIT_INDEX_VERSION,
    IndexTextOptions,
)
from enterprise_pdf_rag.processing.lexical.chart_text_layer import chart_text_layer
from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.page.models import ObjectKind

_BOTH = frozenset({ObjectKind.TABLE, ObjectKind.CHART})


def test_the_default_options_keep_the_one_unit_index_version() -> None:
    assert ONE_UNIT_EACH.lexical_only_kinds == frozenset()
    assert ONE_UNIT_EACH.index_version == INDEX_VERSION


def test_lexical_only_kinds_are_named_by_the_index_version_and_read_back() -> None:
    options = IndexTextOptions(lexical_only_kinds=_BOTH)
    assert options.index_version == f"{UNIT_INDEX_VERSION}:lexical-only-v1=Chart,Table"
    assert IndexTextOptions.from_index_version(options.index_version) == options
    combined = IndexTextOptions(True, True, frozenset({ObjectKind.CHART}))
    assert combined.index_version == (
        f"{UNIT_INDEX_VERSION}:table-row-units-v1+running-lines-unscored-v1+lexical-only-v1=Chart"
    )
    assert IndexTextOptions.from_index_version(combined.index_version) == combined
    # A different set is a different index version, so a different snapshot id.
    assert (
        IndexTextOptions(lexical_only_kinds=frozenset({ObjectKind.TABLE})).index_version
        != options.index_version
    )


@pytest.mark.parametrize(
    "version",
    [
        f"{UNIT_INDEX_VERSION}:lexical-only-v1=",
        f"{UNIT_INDEX_VERSION}:lexical-only-v1=Spreadsheet",
        f"{UNIT_INDEX_VERSION}:lexical-only-v2=Chart",
    ],
)
def test_an_unknown_lexical_only_feature_is_refused(version: str) -> None:
    with pytest.raises(ValueError, match="unknown index version"):
        IndexTextOptions.from_index_version(version)


_CHART = (100.0, 100.0, 400.0, 300.0)
_SPANS = (
    TextSpan("s-title", "VONB margin", (110.0, 105.0, 200.0, 117.0)),
    TextSpan("s-legend", "1H26", (300.0, 105.0, 330.0, 117.0)),
    TextSpan("s-blank", "   ", (340.0, 105.0, 350.0, 117.0)),
    TextSpan("s-v1", "1,168", (120.0, 150.0, 150.0, 162.0)),
    TextSpan("s-v2", "(294)", (200.0, 150.0, 230.0, 162.0)),
    TextSpan("s-v3", "8.2%", (260.0, 150.0, 290.0, 162.0)),
    TextSpan("s-axis-a", "Agency", (120.0, 280.0, 160.0, 292.0)),
    TextSpan("s-axis-b", "Partnership", (200.0, 280.0, 260.0, 292.0)),
    # Outside the chart: the page's body text and a footnote below it.
    TextSpan("s-body", "Revenue grew 12% in 2025", (100.0, 60.0, 300.0, 72.0)),
    TextSpan("s-foot", "Source: management accounts", (100.0, 320.0, 300.0, 332.0)),
    # Straddling the edge with its centre outside: not the chart's.
    TextSpan("s-edge", "99", (395.0, 200.0, 425.0, 212.0)),
)


def test_a_chart_reads_the_spans_inside_its_rectangle_in_reading_order() -> None:
    projection = chart_text_layer(3, _SPANS, _CHART)
    assert projection.page_index == 3
    assert projection.text == "VONB margin 1H26\n1,168 (294) 8.2%\nAgency Partnership"
    # Every line keeps the spans it was read from: the locator of each word and number.
    assert [line.span_ids for line in projection.lines] == [
        ("s-title", "s-legend"),
        ("s-v1", "s-v2", "s-v3"),
        ("s-axis-a", "s-axis-b"),
    ]
    assert projection.span_ids == (
        "s-title",
        "s-legend",
        "s-v1",
        "s-v2",
        "s-v3",
        "s-axis-a",
        "s-axis-b",
    )


def test_every_number_in_the_projection_is_printed_verbatim_by_one_of_its_spans() -> None:
    projection = chart_text_layer(0, _SPANS, _CHART)
    printed = {span.span_id: span.text for span in _SPANS}
    for line in projection.lines:
        texts = [printed[span_id] for span_id in line.span_ids]
        for number in re.findall(r"\S*\d\S*", line.text):
            assert any(number in text for text in texts), number
    # Nothing outside the rectangle, nothing normalised: ``1,168`` keeps its separator.
    assert "12%" not in projection.text and "99" not in projection.text.split()
    assert "1168" not in projection.text


def test_a_chart_with_no_printed_text_projects_nothing() -> None:
    projection = chart_text_layer(0, _SPANS, (500.0, 500.0, 600.0, 600.0))
    assert projection.text == "" and projection.lines == () and projection.span_ids == ()
