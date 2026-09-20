"""Real AIA source extraction; no synthetic source replaces this acceptance."""

from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter

SAMPLE = Path("data/samples/aia-group-2026-interim-results-presentation.pdf")
EXPECTED_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
DIVIDEND_BBOX = (731.5, 145.0, 944.2, 385.0)


@pytest.fixture(scope="module")
def source_pdf() -> bytes:
    if not SAMPLE.is_file():
        pytest.skip(
            "Optional real AIA corpus is absent; provision the pinned local PDF to run source acceptance. No download is performed."
        )
    source = SAMPLE.read_bytes()
    assert sha256(source).hexdigest() == EXPECTED_SHA256
    return source


def test_real_aia_pages_preserve_native_svg_and_coordinate_text_sidecars(
    source_pdf: bytes,
) -> None:
    result = PdfspineDocumentAdapter().extract_document(source_pdf)

    assert len(result.pages) == 71
    assert tuple(page.page_index for page in result.pages) == tuple(range(71))
    assert all(
        (page.width, page.height, page.rotation) == (960.0, 540.0, 0)
        for page in result.pages
    )
    document = pdfspine.open(stream=source_pdf, filetype="pdf")
    try:
        assert result.pages[9].native_svg == document.load_page(9).get_svg_image(
            text_as_path=False
        )
    finally:
        document.close()
    page = result.pages[24]
    value = next(span for span in page.text_spans if span.text == "49.00")
    assert value.bbox == (
        777.17,
        206.14999999999998,
        807.3259999999999,
        219.52999999999997,
    )
    assert value.font
    assert value.size == 12.0
    assert value.direction == (1.0, 0.0)
    assert any("glyph" in warning for warning in page.warnings)
    raster_page = result.pages[18]
    assert (
        len(
            ElementTree.fromstring(raster_page.native_svg).findall(
                ".//{http://www.w3.org/2000/svg}image"
            )
        )
        == 6
    )
    assert any("mixed vector/raster" in warning for warning in raster_page.warnings)

    region = PdfspineDocumentAdapter().extract_region(
        source_pdf, page_index=24, bbox=DIVIDEND_BBOX
    )
    assert region.native_svg == page.native_svg
    by_id = {span.span_id: span for span in page.text_spans}
    assert all(by_id[span.span_id] == span for span in region.text_spans)


def test_real_dividend_region_keeps_page_coordinates_and_only_intersecting_text(
    source_pdf: bytes,
) -> None:
    result = PdfspineDocumentAdapter().extract_region(
        source_pdf, page_index=24, bbox=DIVIDEND_BBOX
    )

    assert result.bbox == DIVIDEND_BBOX
    assert result.page_index == 24
    labels = {span.text for span in result.text_spans}
    assert {
        "49.00",
        "53.90",
        "1H25",
        "1H26",
        "(HK cents)",
        "Interim Dividend per share ",
        "+10%",
    } <= labels
    assert all(
        span.bbox[2] > DIVIDEND_BBOX[0] and span.bbox[0] < DIVIDEND_BBOX[2]
        for span in result.text_spans
    )
    assert all(
        span.bbox[3] > DIVIDEND_BBOX[1] and span.bbox[1] < DIVIDEND_BBOX[3]
        for span in result.text_spans
    )
    assert result.native_svg != result.cropped_svg
    assert any("not verified" in warning for warning in result.warnings)


def test_any_native_export_failure_names_the_page_and_aborts_document(
    monkeypatch: pytest.MonkeyPatch,
    source_pdf: bytes,
) -> None:
    original = pdfspine.Page.get_svg_image

    def fail_on_tenth_page(page: pdfspine.Page, *, text_as_path: bool) -> str:
        if page.number == 9:
            raise pdfspine.PdfUnsupportedError("unsupported paint operation")
        return original(page, text_as_path=text_as_path)

    monkeypatch.setattr(pdfspine.Page, "get_svg_image", fail_on_tenth_page)
    with pytest.raises(
        ValueError, match="Page index 9 extraction failed: unsupported paint operation"
    ):
        PdfspineDocumentAdapter().extract_document(source_pdf)


def test_rotated_actual_page_is_explicitly_unsupported(source_pdf: bytes) -> None:
    document = pdfspine.open(stream=source_pdf, filetype="pdf")
    try:
        document.load_page(24).set_rotation(90)
        rotated_source = document.tobytes()
    finally:
        document.close()

    with pytest.raises(
        ValueError, match="Page index 24 extraction failed: Rotated pages"
    ):
        PdfspineDocumentAdapter().extract_region(
            rotated_source, page_index=24, bbox=DIVIDEND_BBOX
        )


@pytest.mark.parametrize("page_index", [-1, 71])
def test_region_rejects_page_outside_actual_document(
    page_index: int, source_pdf: bytes
) -> None:
    with pytest.raises(ValueError, match=f"Page index {page_index} is out of range"):
        PdfspineDocumentAdapter().extract_region(
            source_pdf, page_index=page_index, bbox=DIVIDEND_BBOX
        )
