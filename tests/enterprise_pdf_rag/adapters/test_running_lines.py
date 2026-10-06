"""Running headers / footers are read from the pages' own spans, conservatively."""

from enterprise_pdf_rag.adapters.running_lines import running_spans
from ragspine.extraction.evidence.document.models import TextSpan

_HEIGHT = 600.0


def _span(span_id: str, text: str, top: float, left: float = 20.0) -> TextSpan:
    return TextSpan(span_id, text, (left, top, left + 8.0 * len(text), top + 8.0), size=8.0)


def _page(page: int, *body: TextSpan) -> tuple[int, float, tuple[TextSpan, ...]]:
    return (
        page,
        _HEIGHT,
        (
            _span(f"h{page}", "Acme Interim Report 2024", 12.0),
            _span(f"b{page}", " ", 12.0, left=300.0),
            *body,
            _span(f"n{page}", str(page + 1), 585.0, left=200.0),
        ),
    )


_PAGES = tuple(
    _page(
        page,
        _span(f"t{page}", f"Body text {page}", 100.0),
        _span(f"k{page}", str(page + 40), 300.0),
    )
    for page in range(3)
)


def test_a_header_and_a_margin_page_number_are_running_and_blank_spans_do_not_count() -> None:
    running = running_spans(_PAGES)
    assert running.covers(0, ("h0",))
    assert running.covers(0, ("h0", "b0"))  # a blank span on the line is not printed text
    assert running.covers(2, ("n2",))  # "3" differs per page but is a margin page number


def test_a_member_that_also_prints_anything_else_is_left_alone() -> None:
    running = running_spans(_PAGES)
    assert not running.covers(0, ("h0", "t0"))
    assert not running.covers(1, ("t1",))
    assert not running.covers(1, ("k1",))  # a lone number in the body is not a page number
    assert not running.covers(0, ("b0",))  # nothing printed, nothing to drop


def test_a_line_on_too_few_pages_is_not_running() -> None:
    pages = (*_PAGES, *(_page(page) for page in range(3, 10)))
    once = (10, _HEIGHT, (_span("once", "Only here", 12.0, left=200.0),))
    running = running_spans((*pages, once))
    assert running.covers(0, ("h0",))
    assert not running.covers(10, ("once",))
