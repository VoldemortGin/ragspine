"""Conservative stroke visibility must not fill the interior of a frame."""

import pytest

from enterprise_pdf_rag.adapters.stroke_visibility import stroke_envelope


def test_surrounding_rectangle_does_not_occlude_its_empty_interior() -> None:
    envelope = stroke_envelope(
        (
            ("M", (0.0, 0.0)),
            ("L", (100.0, 0.0)),
            ("L", (100.0, 100.0)),
            ("L", (0.0, 100.0)),
            ("Z", ()),
        ),
        width=1.0,
    )

    assert not envelope.intersects((20.0, 20.0, 80.0, 80.0))
    assert envelope.intersects((-0.2, 49.0, 0.2, 51.0))
    assert envelope.style_status == "bounded_unknown_style"


def test_rounded_frame_is_proved_outside_region_by_its_individual_segments() -> None:
    envelope = stroke_envelope(
        (
            ("M", (0.0, 10.0)),
            ("C", (0.0, 4.0, 4.0, 0.0, 10.0, 0.0)),
            ("L", (90.0, 0.0)),
            ("C", (96.0, 0.0, 100.0, 4.0, 100.0, 10.0)),
            ("L", (100.0, 90.0)),
            ("C", (100.0, 96.0, 96.0, 100.0, 90.0, 100.0)),
            ("L", (10.0, 100.0)),
            ("C", (4.0, 100.0, 0.0, 96.0, 0.0, 90.0)),
            ("Z", ()),
        ),
        width=1.0,
        region=(20.0, 20.0, 80.0, 80.0),
    )

    assert not envelope.intersects((20.0, 20.0, 80.0, 80.0))


def test_width_uses_the_similarity_ctm_at_stroke_time() -> None:
    envelope = stroke_envelope(
        (("M", (0.0, 0.0)), ("L", (10.0, 0.0))),
        width=1.0,
        stroke_ctm=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0),
    )

    assert envelope.intersects((4.0, 0.9, 6.0, 1.0))
    assert not envelope.intersects((4.0, 1.01, 6.0, 2.0))


def test_dashed_strokes_cannot_be_treated_as_solid_visible_source() -> None:
    with pytest.raises(ValueError, match="unsupported_stroke_dash"):
        stroke_envelope(
            (("M", (0.0, 0.0)), ("L", (10.0, 0.0))),
            width=1.0,
            dashes="[2 2] 0",
        )


def test_overflowing_envelope_is_rejected_instead_of_becoming_unbounded() -> None:
    with pytest.raises(ValueError, match="nonfinite_stroke_envelope"):
        stroke_envelope(
            (("M", (1.0e308, 0.0)), ("L", (1.1e308, 0.0))),
            width=1.5e308,
        )


def test_invalid_region_cannot_hide_a_curve_intersection() -> None:
    with pytest.raises(ValueError, match="invalid_stroke_region"):
        stroke_envelope(
            (("M", (0.0, 0.0)), ("C", (0.0, 4.0, 4.0, 10.0, 10.0, 10.0))),
            width=1.0,
            region=(float("nan"), 0.0, 20.0, 20.0),
        )


def test_acute_join_bound_includes_a_possible_long_miter() -> None:
    envelope = stroke_envelope(
        (("M", (-10.0, 0.0)), ("L", (0.0, 0.0)), ("L", (-10.0, 2.0))),
        width=1.0,
    )

    assert envelope.intersects((3.0, 3.0, 3.1, 3.1))


def test_closed_path_checks_the_join_from_last_segment_to_first() -> None:
    envelope = stroke_envelope(
        (
            ("M", (0.0, 0.0)),
            ("L", (-10.0, 0.0)),
            ("L", (-10.0, 2.0)),
            ("Z", ()),
        ),
        width=1.0,
    )

    assert envelope.intersects((3.0, 3.0, 3.1, 3.1))


@pytest.mark.parametrize(
    "commands",
    [
        (("M", (0.0, 0.0)), ("L", (0.0, 0.0))),
        (("M", (0.0, 0.0)), ("L", (10.0, 0.0)), ("L", (0.0, 0.0))),
        (("M", (0.0, 0.0)), ("C", (4.0, 0.0, -4.0, 0.0, 0.0, 0.0))),
    ],
)
def test_zero_reverse_or_cusp_tangents_are_rejected(
    commands: tuple[tuple[str, tuple[float, ...]], ...],
) -> None:
    with pytest.raises(ValueError, match=r"degenerate|unbounded"):
        stroke_envelope(commands, width=1.0, region=(20.0, 20.0, 80.0, 80.0))


def test_curve_bound_touching_the_region_is_rejected() -> None:
    with pytest.raises(ValueError, match="stroke_curve_not_proved_outside_region"):
        stroke_envelope(
            (("M", (0.0, 0.0)), ("C", (0.0, 4.0, 4.0, 10.0, 10.0, 10.0))),
            width=1.0,
            region=(4.0, 4.0, 6.0, 6.0),
        )


@pytest.mark.parametrize(
    "matrix",
    [(2.0, 0.0, 0.0, 1.0, 0.0, 0.0), (1.0, 0.1, 0.0, 1.0, 0.0, 0.0)],
)
def test_shear_and_anisotropic_stroke_transforms_are_rejected(
    matrix: tuple[float, float, float, float, float, float],
) -> None:
    with pytest.raises(ValueError, match="unsupported_stroke_transform"):
        stroke_envelope(
            (("M", (0.0, 0.0)), ("L", (10.0, 0.0))),
            width=1.0,
            stroke_ctm=matrix,
        )
