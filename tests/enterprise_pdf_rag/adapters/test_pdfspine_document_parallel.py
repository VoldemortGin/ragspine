"""Per-page parallel extraction (ADR 0040): same pages, same order, same errors as serial."""

import threading
import time
from collections.abc import Callable

import pdfspine
import pytest

from enterprise_pdf_rag.adapters import pdfspine_document
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from ragspine.common.evidence.configs import get_settings
from ragspine.extraction.evidence.document.models import PageExtraction
from ragspine.extraction.evidence.document.text_layer import diagnose_text_layer


def _pdf(pages: int) -> bytes:
    document = pdfspine.open()
    for _ in range(pages):
        document.new_page(width=100, height=200)
    return document.tobytes()


def _fake_page(hook: Callable[[int], None] | None) -> Callable[..., PageExtraction]:
    def extract(_page: pdfspine.Page, *, source_digest: str, page_index: int) -> PageExtraction:
        if hook is not None:
            hook(page_index)
        return PageExtraction(
            page_index,
            100.0,
            200.0,
            0,
            f"<svg>{page_index}</svg>",
            (),
            (source_digest[:8],),
            diagnose_text_layer((), drawing_count=0),
        )

    return extract


def _install(monkeypatch: pytest.MonkeyPatch, hook: Callable[[int], None] | None = None) -> None:
    monkeypatch.setattr(pdfspine_document, "_extract_page", _fake_page(hook))


def test_parallel_and_serial_extract_the_same_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    pdf = _pdf(12)
    serial = PdfspineDocumentAdapter(page_workers=1).extract_document(pdf)
    parallel = PdfspineDocumentAdapter(page_workers=4).extract_document(pdf)

    assert parallel == serial
    assert [page.page_index for page in parallel.pages] == list(range(12))


def test_pages_are_filled_by_index_even_when_they_finish_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished: list[int] = []

    def slow_early_pages(index: int) -> None:
        time.sleep(0.2 - 0.02 * index)  # page 0 finishes last
        finished.append(index)

    _install(monkeypatch, slow_early_pages)
    result = PdfspineDocumentAdapter(page_workers=8).extract_document(_pdf(8))

    assert finished != sorted(finished)
    assert [page.page_index for page in result.pages] == list(range(8))
    assert [page.native_svg for page in result.pages] == [f"<svg>{i}</svg>" for i in range(8)]


def test_one_worker_never_starts_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: set[int] = set()
    _install(monkeypatch, lambda _i: seen.add(threading.get_ident()))
    PdfspineDocumentAdapter(page_workers=1).extract_document(_pdf(5))

    assert seen == {threading.get_ident()}


def test_several_workers_do_use_other_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: set[int] = set()
    barrier = threading.Barrier(2, timeout=5)

    def meet(_index: int) -> None:
        seen.add(threading.get_ident())
        barrier.wait()

    _install(monkeypatch, meet)
    PdfspineDocumentAdapter(page_workers=2).extract_document(_pdf(2))

    assert threading.get_ident() not in seen and len(seen) == 2


@pytest.mark.parametrize("workers", [1, 4])
def test_a_failing_page_raises_the_serial_error_and_the_document_still_closes(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    def fail(index: int) -> None:
        if index in (3, 6):
            raise pdfspine.PdfError(f"boom {index}")

    _install(monkeypatch, fail)
    opened: list[pdfspine.Document] = []
    real_open = pdfspine_document.open_pdf

    def spy(pdf: bytes) -> pdfspine.Document:
        opened.append(real_open(pdf))
        return opened[-1]

    monkeypatch.setattr(pdfspine_document, "open_pdf", spy)
    with pytest.raises(ValueError, match=r"^Page index 3 extraction failed: boom 3$") as caught:
        PdfspineDocumentAdapter(page_workers=workers).extract_document(_pdf(9))

    assert isinstance(caught.value.__cause__, pdfspine.PdfError)
    with pytest.raises(Exception):  # noqa: B017 - any "closed document" error
        opened[0].load_page(0)


def test_the_worker_count_defaults_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdfspine_document.os, "cpu_count", lambda: 16)
    get_settings.cache_clear()
    try:
        monkeypatch.delenv("APP_PDF_EXTRACT_WORKERS", raising=False)
        assert pdfspine_document._page_workers(None, 71) == 4
        assert pdfspine_document._page_workers(None, 2) == 2
        monkeypatch.setenv("APP_PDF_EXTRACT_WORKERS", "1")
        get_settings.cache_clear()
        assert pdfspine_document._page_workers(None, 71) == 1
        monkeypatch.setenv("APP_PDF_EXTRACT_WORKERS", "8")
        get_settings.cache_clear()
        assert pdfspine_document._page_workers(None, 71) == 8
    finally:
        get_settings.cache_clear()
