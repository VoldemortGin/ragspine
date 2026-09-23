"""Hand-built pdfspine observations; the glyph metrics are the authored ASCII fixture font's."""

import pdfspine

from ragspine.extraction.evidence.document.models import Bounds
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaSourceObservation,
    Matrix,
    ObservedChar,
    ObservedPath,
    ObservedRun,
    Point,
)
from ragspine.extraction.evidence.objects.formulas.formula_rules import IDENTITY

PAGE_HEIGHT = 160.0
SOURCE_SHA256 = "0123456789abcdef" * 4
PDFSPINE_TAG = f"pdfspine/{pdfspine.__version__}"
# Probed from tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf through pdfspine:
# every glyph advances 0.6 * size and its box spans 0.8 * size above the baseline to
# 0.2 * size below it, so synthetic geometry matches what the real fixture PDF reports.
ADVANCE = 0.6
ASCENT = 0.8
DESCENT = 0.2


def run(
    span_id: str,
    text: str,
    *,
    origin: Point,
    size: float,
    rise: float = 0.0,
    ctm: Matrix = IDENTITY,
    chars: tuple[ObservedChar, ...] | None = None,
    font: str = "Authored",
    flags: int = 0,
    direction: Point = (1.0, 0.0),
) -> ObservedRun:
    """One span; ``text_matrix[5]`` is set so ``rise_of`` recovers exactly ``rise``."""
    left, baseline = origin
    top, bottom = baseline - ASCENT * size, baseline + DESCENT * size
    boxes = chars
    if boxes is None:
        boxes = tuple(
            ObservedChar(
                char,
                (
                    left + index * ADVANCE * size,
                    top,
                    left + (index + 1) * ADVANCE * size,
                    bottom,
                ),
            )
            for index, char in enumerate(text)
        )
    bbox: Bounds = (left, top, left + len(text) * ADVANCE * size, bottom)
    text_matrix: Matrix = (1.0, 0.0, 0.0, 1.0, left, PAGE_HEIGHT - baseline - rise)
    return ObservedRun(
        span_id, text, bbox, origin, size, font, direction, ctm, text_matrix, flags, boxes
    )


def line(index: int, y: float, x0: float, x1: float, width: float = 0.8) -> ObservedPath:
    """A stroked horizontal rule in page top-left coordinates, as ``observe_formula`` flips them."""
    return ObservedPath(index, "s", width, False, (("l", ((x0, y), (x1, y))),))


def observation(
    runs: tuple[ObservedRun, ...],
    paths: tuple[ObservedPath, ...],
    *,
    bbox: Bounds,
    page_height: float = PAGE_HEIGHT,
) -> FormulaSourceObservation:
    return FormulaSourceObservation(
        "formula-source-observation-v1",
        PDFSPINE_TAG,
        SOURCE_SHA256,
        0,
        page_height,
        bbox,
        runs,
        paths,
    )


def anchor(bbox: Bounds) -> SourceAnchor:
    return SourceAnchor("source-revision-1", SOURCE_SHA256, 0, bbox)
