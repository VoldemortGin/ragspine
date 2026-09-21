"""Match a chart's strings against short runs of adjacent source text observations.

This is the figure-side sibling of the ADR 0013 page-metadata evidence window
(``processing/page_metadata.py``: ``fold_whitespace``, ``MAX_EVIDENCE_SPANS`` and
``_verify``). Both answer the same question — "is this string printed by the cited
occurrence plus at most two following ones, whitespace-folded and case preserved?" —
and both keep the source text, never the model's string.

Two deliberate tightenings for figures:

* **Equality, not substring.** Page metadata may quote a fragment of a line; a chart
  label *is* the printed run, so the window must print exactly it.
* **Bbox adjacency.** A page's spans arrive in one linear text flow, so consecutive
  spans really are consecutive text. A figure's observations are scattered over a
  plot area, where "the next span in reading order" can be an axis tick on the far
  side of the chart. Consecutive window members must therefore be geometrically
  adjacent: on the same printed line, or wrapped onto the next one.

``match_source_value`` applies the same windows to a printed number, reading it with
``validation.explicit_number`` and allowing the unit to be printed inside the same
window (``33%``, ``$965m``). Proving that a number is printed verbatim inside the
figure is *not* proving which category it belongs to; that association stays the
model's assertion, and only the geometry + source-paint proofs settle it (ADR 0008).
"""

from collections.abc import Iterator, Sequence
from decimal import Decimal

from enterprise_pdf_rag.figures.models import EvidenceKind, SvgElement
from enterprise_pdf_rag.figures.validation import explicit_number

# A label may wrap over a few printed lines ("Traditional" / "Protection"): a window is
# the cited occurrence plus at most this many occurrences in all. Same bound as the page
# metadata evidence window, for the same reason — beyond it, "adjacent" stops meaning one label.
MAX_LABEL_SPANS = 3
# Adjacency thresholds, in the source anchor's page-top-left points. Overlap fractions say
# "these two boxes share a line / a column"; the gap bounds say "nothing was printed between
# them". Gaps are measured against the smaller box height, which tracks the font size, so the
# rule holds at any type scale. A small negative gap absorbs kerning and rounding.
_SAME_LINE_OVERLAP = 0.5
_WRAPPED_LINE_OVERLAP = 0.3
_MAX_GAP = 0.6
_MIN_GAP = -1.0


def fold_whitespace(text: str) -> str:
    return " ".join(text.split())


def _open_bracket(folded: str) -> int | None:
    """Where the one balanced group that closes ``folded`` opens, or ``None``."""
    if not folded.endswith(")"):
        return None
    depth = 0
    for index in range(len(folded) - 1, -1, -1):
        if folded[index] == ")":
            depth += 1
        elif folded[index] == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def without_trailing_parenthetical(text: str) -> str | None:
    """``"UFSG per share (US cents)"`` -> ``"UFSG per share"``; ``None`` when there is none.

    Exactly one balanced trailing group is removed. Nothing else is rewritten, so this
    stays a second verbatim attempt rather than the start of fuzzy matching.
    """
    folded = fold_whitespace(text)
    index = _open_bracket(folded)
    return None if index is None else fold_whitespace(folded[:index]) or None


def trailing_parenthetical(text: str) -> str | None:
    """``"VONB ($m)"`` -> ``"$m"``: what that one trailing group prints, without its brackets."""
    folded = fold_whitespace(text)
    index = _open_bracket(folded)
    return None if index is None else fold_whitespace(folded[index + 1 : -1]) or None


def _is_source_occurrence(element: SvgElement) -> bool:
    return (
        element.evidence_kind is EvidenceKind.SOURCE_TEXT_OBSERVATION
        and element.source_span_id is not None
    )


def _adjacent(previous: SvgElement, following: SvgElement) -> bool:
    """True when the two boxes read as one continuous label, on a line or wrapped onto the next."""
    left, top, right, bottom = previous.anchor.bbox
    next_left, next_top, next_right, next_bottom = following.anchor.bbox
    height = min(bottom - top, next_bottom - next_top)
    width = min(right - left, next_right - next_left)
    if (
        min(bottom, next_bottom) - max(top, next_top) > _SAME_LINE_OVERLAP * height
        and _MIN_GAP <= next_left - right <= _MAX_GAP * height
    ):
        return True
    if min(right, next_right) - max(left, next_left) > _WRAPPED_LINE_OVERLAP * width:
        return _MIN_GAP <= next_top - bottom <= _MAX_GAP * height
    return False


def windows(
    elements: Sequence[SvgElement], cited: Sequence[str]
) -> Iterator[tuple[SvgElement, ...]]:
    """Every admissible window over the cited occurrences, narrowest first, then earliest.

    ``elements`` is the figure's observations in page reading order. A window is a
    contiguous run of the *cited* occurrences in that order, one to ``MAX_LABEL_SPANS``
    wide, all of them source observations and each geometrically adjacent to the next.
    Unresolved, duplicated or empty citations yield nothing, so every caller fails closed.
    """
    if not cited or len(set(cited)) != len(cited):
        return
    positions = {element.element_id: index for index, element in enumerate(elements)}
    if any(element_id not in positions for element_id in cited):
        return
    ordered = tuple(elements[index] for index in sorted(positions[item] for item in cited))
    for width in range(1, MAX_LABEL_SPANS + 1):
        for start in range(len(ordered) - width + 1):
            window = ordered[start : start + width]
            if all(_is_source_occurrence(element) for element in window) and all(
                _adjacent(window[index], window[index + 1]) for index in range(width - 1)
            ):
                yield window


def window_text(window: Sequence[SvgElement]) -> str:
    return fold_whitespace(" ".join(element.text for element in window))


def match_source_label(
    elements: Sequence[SvgElement],
    text: str,
    cited: Sequence[str],
) -> tuple[SvgElement, ...] | None:
    """The smallest window of cited, adjacent source occurrences that prints ``text``.

    The narrowest — then the earliest — match wins, so the result is deterministic.
    Anything non-adjacent, native or merely similar returns ``None``: the caller must
    fail that string closed, never repair it.
    """
    folded = fold_whitespace(text)
    if not folded:
        return None
    for window in windows(elements, cited):
        if window_text(window) == folded:
            return window
    return None


def _numeric_readings(text: str, unit: str) -> Iterator[tuple[str, bool]]:
    """``text`` read as a number, with the flag saying whether ``unit`` was printed with it.

    Unit-bearing readings come first so ``33%`` is recorded as carrying its ``%`` rather
    than as a bare ``33`` that still needs one.
    """
    if unit:
        if text.endswith(unit):
            yield text[: -len(unit)].strip(), True
        if text.startswith(unit):
            yield text[len(unit) :].strip(), True
        if len(unit) > 1 and text.startswith(unit[0]) and text.endswith(unit[1:]):
            yield text[1 : len(text) - len(unit) + 1].strip(), True  # ``$965m`` for ``$m``
    yield text, False


def read_printed_value(text: str, unit: str) -> tuple[Decimal, bool] | None:
    """The number ``text`` prints, and whether it prints ``unit`` with it. ``None`` if it is not one.

    One rule, used both when a point is qualified and when its answer is re-read, so a
    projection and its verification can never disagree about what a window printed.
    """
    for reading, carries_unit in _numeric_readings(text, fold_whitespace(unit)):
        printed = explicit_number(reading)
        if printed is not None:
            return printed, carries_unit
    return None


def match_source_value(
    elements: Sequence[SvgElement],
    value: Decimal,
    unit: str,
    cited: Sequence[str],
) -> tuple[tuple[SvgElement, ...], Decimal, bool] | None:
    """The smallest cited window that prints ``value``, and whether it prints ``unit`` too.

    The window's own text is what is read — ``explicit_number`` accepts a printed number,
    an accounting negative and thousands separators and rejects everything else, so a
    window has to *be* the number rather than merely contain one. The returned ``Decimal``
    is the source's reading of it, not the model's, and the flag says whether the unit
    still needs a window of its own.
    """
    for window in windows(elements, cited):
        reading = read_printed_value(window_text(window), unit)
        if reading is not None and reading[0] == value:
            return window, reading[0], reading[1]
    return None
