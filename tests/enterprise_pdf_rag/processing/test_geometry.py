"""Containment between a model-rendered region and canonical source geometry."""

from enterprise_pdf_rag.processing.geometry import COORDINATE_TOLERANCE, contains

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
