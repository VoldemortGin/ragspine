"""Direct label geometry never supplies a value from a bar's height."""

from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_pdf_rag.adapters.bar_geometry import (
    NativeBarPaint,
    match_direct_bar_labels,
    match_visible_bar_labels,
)
from enterprise_pdf_rag.adapters.source_paint_bar import BarVectorPaint
from enterprise_pdf_rag.documents.models import TextSpan


def bars() -> tuple[NativeBarPaint, ...]:
    return tuple(
        NativeBarPaint(
            name,
            (
                ("M", (x, top)),
                ("L", (x + 20.0, top)),
                ("L", (x + 20.0, 100.0)),
                ("L", (x, 100.0)),
                ("Z", ()),
            ),
        )
        for name, x, top in (
            ("first", 20.0, 40.0),
            ("middle", 60.0, 50.0),
            ("last", 100.0, 60.0),
        )
    )


def spans() -> tuple[TextSpan, ...]:
    return (
        TextSpan("first-value", "15%", (23.0, 28.0, 37.0, 38.0)),
        TextSpan("last-value", "6%", (103.0, 48.0, 117.0, 58.0)),
        TextSpan("first-period", "1H21", (23.0, 104.0, 37.0, 114.0)),
        TextSpan("middle-period", "1H22", (63.0, 104.0, 77.0, 114.0)),
        TextSpan("last-period", "1H23", (103.0, 104.0, 117.0, 114.0)),
    )


def fills() -> tuple[BarVectorPaint, ...]:
    return tuple(
        BarVectorPaint(
            bar.native_path_ref,
            index,
            index,
            "fill",
            bar.commands,
            bar.bbox,
            (),
            "#113355",
            255,
            "nonzero",
            (0.0, 0.0, 140.0, 130.0),
        )
        for index, bar in enumerate(bars())
    )


def test_visible_bar_roles_close_over_source_paints_without_height_inference() -> None:
    result = match_visible_bar_labels(
        vectors=fills(), glyphs=(), spans=spans(), region=(0.0, 0.0, 140.0, 130.0)
    )
    assert tuple(point.value for point in result.geometry.points) == (
        Decimal("15"),
        None,
        Decimal("6"),
    )
    assert result.roles == (("first", "bar"), ("middle", "bar"), ("last", "bar"))


def auxiliary_paints() -> tuple[BarVectorPaint, ...]:
    first = fills()[0]
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (
        replace(
            first,
            native_path_ref="outline",
            kind="stroke",
            paint_order=10,
            width=0.5,
            stroke_ctm=identity,
        ),
        replace(
            first,
            native_path_ref="baseline",
            kind="stroke",
            paint_order=11,
            width=0.5,
            stroke_ctm=identity,
            commands=(("M", (10.0, 100.0)), ("L", (130.0, 100.0))),
            bounds=(10.0, 100.0, 130.0, 100.0),
        ),
        replace(
            first,
            native_path_ref="break",
            kind="stroke",
            paint_order=12,
            width=0.5,
            stroke_ctm=identity,
            commands=(("M", (15.0, 90.0)), ("L", (45.0, 85.0))),
            bounds=(15.0, 85.0, 45.0, 90.0),
        ),
        replace(
            first,
            native_path_ref="arrow",
            paint_order=13,
            commands=(
                ("M", (45.0, 25.0)),
                ("L", (91.0, 31.0)),
                ("L", (90.9, 31.7)),
                ("L", (44.9, 25.7)),
                ("L", (45.0, 25.0)),
                ("M", (90.0, 28.0)),
                ("L", (96.0, 32.0)),
                ("L", (89.0, 35.0)),
                ("Z", ()),
            ),
            bounds=(44.9, 25.0, 96.0, 35.0),
        ),
    )


def test_finite_auxiliary_roles_preserve_labels_but_grant_no_arrow_claim() -> None:
    result = match_visible_bar_labels(
        vectors=(*fills(), *auxiliary_paints()),
        glyphs=(),
        spans=spans(),
        region=(0.0, 0.0, 140.0, 130.0),
    )
    assert dict(result.roles) == {
        "first": "bar",
        "middle": "bar",
        "last": "bar",
        "outline": "bar_outline",
        "baseline": "baseline",
        "break": "bar_interruption",
        "arrow": "nonsemantic_arrow",
    }


def test_horizontal_baseline_inside_clip_is_a_supported_zero_height_path() -> None:
    baseline = replace(auxiliary_paints()[1], clips=((0.0, 0.0, 140.0, 130.0),))
    result = match_visible_bar_labels(
        vectors=(*fills(), baseline),
        glyphs=(),
        spans=spans(),
        region=(0.0, 0.0, 140.0, 130.0),
    )
    assert dict(result.roles)["baseline"] == "baseline"


@pytest.mark.parametrize(
    "role", ["white_cover", "thick_break", "unexpected_line", "clipped_bar"]
)
def test_unexplained_or_relation_obscuring_paint_is_not_whitelisted(role: str) -> None:
    vectors = fills()
    if role == "white_cover":
        cover = replace(
            vectors[0],
            native_path_ref="occluder",
            paint_order=50,
            color="#ffffff",
            commands=(
                ("M", (22.0, 45.0)),
                ("L", (38.0, 45.0)),
                ("L", (38.0, 95.0)),
                ("L", (22.0, 95.0)),
                ("Z", ()),
            ),
            bounds=(22.0, 45.0, 38.0, 95.0),
        )
        vectors = (*vectors, cover)
    elif role == "thick_break":
        vectors = (*vectors, replace(auxiliary_paints()[2], width=18.0))
    elif role == "unexpected_line":
        vectors = (
            *vectors,
            replace(
                auxiliary_paints()[2],
                commands=(("M", (50.0, 20.0)), ("L", (50.0, 100.0))),
                bounds=(50.0, 20.0, 50.0, 100.0),
            ),
        )
    else:
        vectors = (
            replace(vectors[0], clips=((25.0, 0.0, 140.0, 130.0),)),
            *vectors[1:],
        )
    with pytest.raises(
        ValueError,
        match=r"bar_baseline_mismatch|bar_stroke|unexplained_bar_region_paint|bar_rectangle_clipped",
    ):
        match_visible_bar_labels(
            vectors=vectors, glyphs=(), spans=spans(), region=(0.0, 0.0, 140.0, 130.0)
        )


def test_direct_labels_have_unique_bars_and_unlabelled_height_stays_unavailable() -> (
    None
):
    result = match_direct_bar_labels(
        bars=bars(), spans=spans(), region=(0.0, 0.0, 140.0, 130.0)
    )

    assert tuple(point.category.text for point in result.points) == (
        "1H21",
        "1H22",
        "1H23",
    )
    assert tuple(point.value for point in result.points) == (
        Decimal("15"),
        None,
        Decimal("6"),
    )
    assert tuple(point.bar.native_path_ref for point in result.points) == (
        "first",
        "middle",
        "last",
    )
    assert result.points[1].literal is None


def test_one_source_label_cannot_qualify_two_overlapping_bars() -> None:
    duplicate = NativeBarPaint("decoy", bars()[0].commands)
    with pytest.raises(ValueError, match="bar_columns_overlap"):
        match_direct_bar_labels(
            bars=(*bars(), duplicate),
            spans=spans(),
            region=(0.0, 0.0, 140.0, 130.0),
        )


@pytest.mark.parametrize("literal", [">15%", "~15%", "约15%", "15 %"])
def test_unsupported_full_display_cannot_be_reduced_to_an_exact_number(
    literal: str,
) -> None:
    observed = (replace(spans()[0], text=literal), *spans()[1:])
    with pytest.raises(ValueError, match="unsupported_bar_percent_display"):
        match_direct_bar_labels(
            bars=bars(), spans=observed, region=(0.0, 0.0, 140.0, 130.0)
        )


def test_separate_approximation_prefix_cannot_be_ignored() -> None:
    observed = (*spans(), TextSpan("prefix", "~", (17.0, 28.0, 22.0, 38.0)))
    with pytest.raises(ValueError, match="bar_numeric_label_has_adjacent_source_text"):
        match_direct_bar_labels(
            bars=bars(), spans=observed, region=(0.0, 0.0, 140.0, 130.0)
        )


def test_floating_rectangles_do_not_form_a_common_baseline_bar_family() -> None:
    first, middle, last = bars()
    changed = replace(
        middle,
        commands=tuple(
            (
                operation,
                tuple(
                    value - 5.0 if index % 2 else value
                    for index, value in enumerate(values)
                ),
            )
            for operation, values in middle.commands
        ),
    )
    with pytest.raises(ValueError, match="bar_baseline_mismatch"):
        match_direct_bar_labels(
            bars=(first, changed, last),
            spans=spans(),
            region=(0.0, 0.0, 140.0, 130.0),
        )


def test_repeated_source_period_labels_are_ambiguous_for_lookup() -> None:
    observed = (*spans()[:-1], replace(spans()[-1], text="1H21"))
    with pytest.raises(ValueError, match="bar_period_labels_not_unique"):
        match_direct_bar_labels(
            bars=bars(), spans=observed, region=(0.0, 0.0, 140.0, 130.0)
        )
