"""Containment between a model-rendered region and canonical source geometry."""

from enterprise_pdf_rag.processing.geometry import (
    COORDINATE_TOLERANCE,
    Axis,
    Segment,
    contains,
    covering_segments,
    ruling_digest,
    segments_crossing,
)

# A canonical pdfspine span and the same box as a layout model echoes it back: the
# prompt renders ``42.400000000000006`` and the model answers ``42.4``.
_CANONICAL_SPAN = (20.0, 30.400000000000006, 307.9999999999998, 42.400000000000006)
_MODEL_REGION = (20.0, 30.4, 308.0, 42.4)


def test_float_rendering_noise_does_not_unbind_a_span_from_its_region() -> None:
    assert contains(_MODEL_REGION, _CANONICAL_SPAN)
    assert contains(_MODEL_REGION, _MODEL_REGION)


def test_real_overreach_and_degenerate_boxes_are_still_rejected() -> None:
    assert not contains((20.0, 30.4, 308.0, 41.9), _CANONICAL_SPAN)
    assert not contains((20.5, 30.4, 308.0, 42.4), _CANONICAL_SPAN)
    assert not contains((20.0, 30.4, 308.0, 42.4 - 1e-3), _CANONICAL_SPAN)
    assert not contains(_MODEL_REGION, (20.0, 30.4, 20.0, 42.4))
    assert not contains(_MODEL_REGION, (20.0, 42.4, 308.0, 30.4))
    assert 0 < COORDINATE_TOLERANCE < 1e-3
    assert not contains(_MODEL_REGION, _CANONICAL_SPAN, tolerance=0.0)


def _ruling(path_index: int, start: float, end: float) -> Segment:
    return Segment(path_index, 0, "l", Axis.HORIZONTAL, 30.0, start, end, 1.0)


def test_covering_segments_stitches_collinear_pieces_and_rejects_gaps() -> None:
    joined = (_ruling(0, 0.0, 50.0), _ruling(1, 50.3, 100.0))
    assert covering_segments(joined, Axis.HORIZONTAL, 30.0, 0.0, 100.0) == joined
    gapped = (_ruling(0, 0.0, 50.0), _ruling(1, 51.0, 100.0))
    assert covering_segments(gapped, Axis.HORIZONTAL, 30.0, 0.0, 100.0) is None
    inside = (_ruling(0, 0.5, 99.5),)
    assert covering_segments(inside, Axis.HORIZONTAL, 30.0, 0.0, 100.0) == inside
    short = (_ruling(0, 0.6, 99.4),)
    assert covering_segments(short, Axis.HORIZONTAL, 30.0, 0.0, 100.0) is None
    assert covering_segments(joined, Axis.HORIZONTAL, 31.0, 0.0, 100.0) is None


def test_segments_crossing_ignores_touching_ends() -> None:
    touching = _ruling(0, 10.0, 20.0)
    interior = _ruling(1, 25.0, 40.0)
    assert segments_crossing((touching,), Axis.HORIZONTAL, 30.0, 20.0, 60.0) == ()
    assert segments_crossing((interior,), Axis.HORIZONTAL, 30.0, 20.0, 60.0) == (interior,)
    assert segments_crossing((_ruling(2, 60.0, 90.0),), Axis.HORIZONTAL, 30.0, 20.0, 60.0) == ()


def test_ruling_digest_is_order_sensitive_and_stable() -> None:
    first, second = _ruling(0, 0.0, 50.0), _ruling(1, 50.0, 100.0)
    assert len(ruling_digest((first, second))) == 64
    assert ruling_digest((first, second)) == ruling_digest((first, second))
    assert ruling_digest((first, second)) != ruling_digest((second, first))
    assert ruling_digest(()) != ruling_digest((first,))
