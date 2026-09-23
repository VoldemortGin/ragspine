"""Three charts side by side are three different countries, and geometry says which."""

from ragspine.extraction.evidence.page.column_regions import (
    EMPTY,
    PageColumn,
    PageRegionSpan,
    bind_columns,
)

# The real AIA 1H26 p.13 layout, taken from the published store: one page-wide banner over
# three country headings, each standing above its own `VONB ($m)` chart.
BANNER = PageRegionSpan("ASEAN", (28.104, 32.275, 839.508, 62.380))
THAILAND = PageRegionSpan("AIA Thailand", (37.104, 85.714, 122.186, 101.368))
SINGAPORE = PageRegionSpan("AIA Singapore", (347.880, 85.984, 443.829, 101.638))
MALAYSIA = PageRegionSpan("AIA Malaysia", (662.590, 85.862, 748.212, 101.543))
LEFT = PageColumn("chart-left", (38.0, 176.0, 302.0, 335.0))
MIDDLE = PageColumn("chart-middle", (350.0, 176.0, 612.0, 335.0))
RIGHT = PageColumn("chart-right", (664.0, 176.0, 922.0, 335.0))


def test_each_side_by_side_chart_takes_the_heading_standing_over_it() -> None:
    binding = bind_columns((BANNER, THAILAND, SINGAPORE, MALAYSIA), (LEFT, MIDDLE, RIGHT))

    assert binding.page_wide == ("ASEAN",)
    assert binding.regions_for("chart-left") == ("ASEAN", "AIA Thailand")
    assert binding.regions_for("chart-middle") == ("ASEAN", "AIA Singapore")
    assert binding.regions_for("chart-right") == ("ASEAN", "AIA Malaysia")
    # A member that was never offered as a column is not bound at all.
    assert binding.regions_for("some-paragraph") == ()


def test_a_banner_as_wide_as_the_page_names_every_column_and_none_of_them() -> None:
    binding = bind_columns((BANNER, THAILAND, SINGAPORE, MALAYSIA), (LEFT, MIDDLE, RIGHT))

    assert "ASEAN" in binding.regions_for("chart-left")
    assert "ASEAN" not in binding.by_member["chart-left"]


def test_one_chart_on_a_page_is_never_ambiguous_so_nothing_is_bound() -> None:
    assert bind_columns((BANNER, THAILAND), (LEFT,)) is EMPTY


def test_a_heading_standing_over_no_column_gives_up_the_whole_page() -> None:
    stray = PageRegionSpan("AIA Vietnam", (1000.0, 85.0, 1080.0, 101.0))

    assert bind_columns((THAILAND, SINGAPORE, MALAYSIA, stray), (LEFT, MIDDLE, RIGHT)) is EMPTY


def test_a_column_no_heading_names_gives_up_the_whole_page() -> None:
    # Two headings, three charts: the third would silently lose its region, so the page
    # keeps the page-level values it has always had.
    assert bind_columns((BANNER, THAILAND, SINGAPORE), (LEFT, MIDDLE, RIGHT)) is EMPTY


def test_a_heading_over_two_stacked_charts_names_both_of_them() -> None:
    # A 2x2 grid: two headings, four charts, each heading naming its own column.
    top_left = PageColumn("top-left", (38.0, 176.0, 302.0, 260.0))
    bottom_left = PageColumn("bottom-left", (38.0, 280.0, 302.0, 364.0))
    top_middle = PageColumn("top-middle", (350.0, 176.0, 612.0, 260.0))
    bottom_middle = PageColumn("bottom-middle", (350.0, 280.0, 612.0, 364.0))

    binding = bind_columns(
        (THAILAND, SINGAPORE), (top_left, bottom_left, top_middle, bottom_middle)
    )

    assert binding.regions_for("top-left") == ("AIA Thailand",)
    assert binding.regions_for("bottom-left") == ("AIA Thailand",)
    assert binding.regions_for("top-middle") == ("AIA Singapore",)
    assert binding.regions_for("bottom-middle") == ("AIA Singapore",)


def test_a_region_whose_evidence_has_no_rectangle_stays_page_wide() -> None:
    unplaced = PageRegionSpan("ASEAN", None)

    binding = bind_columns((unplaced, THAILAND, SINGAPORE, MALAYSIA), (LEFT, MIDDLE, RIGHT))

    assert binding.page_wide == ("ASEAN",)
    assert binding.regions_for("chart-left") == ("ASEAN", "AIA Thailand")


def test_fewer_than_two_headings_leaves_the_page_alone() -> None:
    assert bind_columns((BANNER, THAILAND), (LEFT, MIDDLE, RIGHT)) is EMPTY
    assert bind_columns((), (LEFT, MIDDLE, RIGHT)) is EMPTY
