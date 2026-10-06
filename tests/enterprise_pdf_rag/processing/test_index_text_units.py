"""A long verbatim-rows table is indexed as row units that repeat its header (pure)."""

import pytest

from enterprise_pdf_rag.processing.index_text import (
    INDEX_VERSION,
    MAX_HEADER_ROWS,
    MAX_UNITS_PER_TABLE,
    IndexTextOptions,
    PageIndexContext,
    table_header_rows,
    table_row_units,
)
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.objects.tables.table_rows import TableRow, TableRowsIR

_ANCHOR = SourceAnchor("a" * 64, "a" * 64, 0, (10.0, 50.0, 390.0, 250.0))
_CONTEXT = PageIndexContext("Acme Report", "Consolidated income statement", None)


def _ir(*rows: tuple[str, ...]) -> TableRowsIR:
    return TableRowsIR(
        "table-1",
        _ANCHOR,
        tuple(
            TableRow(
                f"row-{index}",
                (10.0, 50.0 + index * 10, 390.0, 58.0 + index * 10),
                tuple(f"s{index}-{cell}" for cell in range(len(texts))),
                texts,
            )
            for index, texts in enumerate(rows)
        ),
    )


_STATEMENT = _ir(
    ("Year ended 31 December",),
    ("HK$m", "2024", "2023", "2022"),
    ("Revenue", "1,234,567", "1,100,200", "990,000"),
    ("Cost of sales", "(456,789)", "(400,100)", "(380,000)"),
    ("Other operating income and",),
    ("expenses", "12,345", "(9,876)", "-"),
    ("Total", "790,123", "690,224", "610,000"),
    ("1 Restated.",),
)


def test_header_rows_are_the_rows_before_the_first_figure_and_years_are_not_figures() -> None:
    assert table_header_rows(_STATEMENT) == 2


def test_a_table_whose_first_row_already_prints_a_figure_repeats_only_its_first_row() -> None:
    ir = _ir(("Revenue", "1,234"), ("Cost", "(456)"), ("Total", "778"))
    assert table_header_rows(ir) == 1


def test_a_header_deeper_than_the_cap_falls_back_to_the_first_row() -> None:
    labels = tuple((f"Heading {index}",) for index in range(MAX_HEADER_ROWS + 1))
    assert table_header_rows(_ir(*labels, ("Revenue", "1,234"), ("Cost", "(456)"))) == 1


def test_units_repeat_the_header_and_carry_one_figure_row_with_its_wrapped_label() -> None:
    units = table_row_units(_STATEMENT, _CONTEXT)
    header = "Acme Report | Consolidated income statement"
    head = "Year ended 31 December\nHK$m\t2024\t2023\t2022"
    assert units == (
        f"{header}\n{head}\nRevenue\t1,234,567\t1,100,200\t990,000",
        f"{header}\n{head}\nCost of sales\t(456,789)\t(400,100)\t(380,000)",
        f"{header}\n{head}\nOther operating income and\nexpenses\t12,345\t(9,876)\t-",
        f"{header}\n{head}\nTotal\t790,123\t690,224\t610,000\n1 Restated.",
    )


def test_every_unit_character_is_printed_by_the_table_or_the_page_context() -> None:
    printed = {char for row in _STATEMENT.rows for text in row.texts for char in text}
    printed |= set(_CONTEXT.header()) | {"\n", "\t"}
    for unit in table_row_units(_STATEMENT, _CONTEXT) or ():
        assert set(unit) <= printed


def test_no_context_means_the_units_start_at_the_header_rows() -> None:
    units = table_row_units(_STATEMENT, None)
    assert units is not None and units[0].startswith("Year ended 31 December\nHK$m")


def test_a_table_with_one_figure_row_or_none_is_not_split() -> None:
    assert table_row_units(_ir(("Metric", "2024"), ("Revenue", "1,234")), _CONTEXT) is None
    prose = _ir(("The group reported growth",), ("in revenue during the period",), ("review",))
    assert table_row_units(prose, _CONTEXT) is None


def test_a_very_long_table_is_cut_into_at_most_the_cap_with_consecutive_rows() -> None:
    rows = [("Item", "2024")] + [(f"Line {index}", f"{index},000") for index in range(200)]
    units = table_row_units(_ir(*rows), None)
    assert units is not None and len(units) <= MAX_UNITS_PER_TABLE
    body = [line for unit in units for line in unit.split("\n")[1:]]
    assert body == [f"Line {index}\t{index},000" for index in range(200)]


def test_index_versions_name_the_switches_and_round_trip() -> None:
    assert IndexTextOptions().index_version == INDEX_VERSION == "immutable-cosine-index-v1"
    seen = {INDEX_VERSION}
    for rows in (False, True):
        for running in (False, True):
            options = IndexTextOptions(table_row_units=rows, drop_running_lines=running)
            assert IndexTextOptions.from_index_version(options.index_version) == options
            seen.add(options.index_version)
    assert len(seen) == 4
    with pytest.raises(ValueError, match="index version"):
        IndexTextOptions.from_index_version("immutable-cosine-unit-index-v1:unknown-v9")
