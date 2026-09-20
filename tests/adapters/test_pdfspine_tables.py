"""pdfspine typed slots are the only native table structure source."""

from hashlib import sha256
from pathlib import Path

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.pdfspine_tables import PdfspineTableAdapter
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.table_models import (
    CellContentState,
    SlotState,
)

SAMPLE = Path("data/samples/aia-group-2026-interim-results-presentation.pdf")
EXPECTED_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
P20_SENSITIVITY_CANDIDATE = (654.0, 125.0, 934.0, 466.0)


def _table_pdf(*, page_index: int = 0) -> bytes:
    document = pdfspine.open()
    try:
        for _ in range(page_index):
            document.new_page(width=300, height=180)
        page = document.new_page(width=300, height=180)
        for start, end in (
            ((20, 20), (220, 20)),
            ((20, 20), (20, 120)),
            ((220, 20), (220, 120)),
            ((20, 120), (220, 120)),
            ((20, 70), (220, 70)),
            ((120, 70), (120, 120)),
        ):
            page.draw_line(start, end, width=1)
        page.insert_text((70, 50), "Header", fontsize=12)
        page.insert_text((50, 100), "Left", fontsize=12)
        return document.tobytes()
    finally:
        document.close()


def _page_input(pdf: bytes, *, page_index: int = 0) -> PageInput:
    extracted = PdfspineDocumentAdapter().extract_document(pdf).pages[page_index]
    svg = extracted.native_svg.encode()
    digest = sha256(pdf).hexdigest()
    return PageInput(
        "a" * 64,
        digest,
        page_index,
        extracted.width,
        extracted.height,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", digest, page_index, extracted.text_spans),
    )


@pytest.mark.parametrize("page_index", [0, 20])
def test_native_typed_slots_preserve_merge_and_exact_source_occurrences(
    page_index: int,
) -> None:
    pdf = _table_pdf(page_index=page_index)
    page = _page_input(pdf, page_index=page_index)
    item = LayoutObject(
        "table-object",
        ObjectKind.TABLE,
        (15.0, 15.0, 225.0, 125.0),
        tuple(span.span_id for span in page.text.spans),
        "Model-proposed table region",
        Confidence(None, "layout inference pending"),
    )

    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is not None
    table = result.table
    assert table.source.source_revision == page.source_sha256
    assert table.source.document_sha256 == page.source_sha256
    assert table.source.page_index == page_index
    assert table.source.bbox == (20.0, 20.0, 220.0, 120.0)
    assert table.verification is Verification.PENDING
    assert (table.row_count, table.col_count) == (2, 2)
    assert tuple(cell.text for cell in table.cells) == ("Header", "Left", "")
    assert tuple(cell.content_state for cell in table.cells) == (
        CellContentState.PRESENT,
        CellContentState.PRESENT,
        CellContentState.BLANK,
    )
    assert table.cells[0].col_span == 2
    assert table.slots[0][0].state is SlotState.ORIGIN
    assert table.slots[0][1].state is SlotState.CONTINUATION
    assert table.slots[0][1].origin_cell_id == table.cells[0].cell_id
    assert {source for cell in table.cells for source in cell.source_span_ids} == {
        span.span_id for span in page.text.spans
    }
    assert any(
        f"pdfspine/{pdfspine.__version__}" in diagnostic
        for diagnostic in result.diagnostics
    )
    assert any("source=native" in diagnostic for diagnostic in result.diagnostics)


def test_source_identity_and_table_kind_fail_before_pdf_parsing() -> None:
    pdf = _table_pdf()
    page = _page_input(pdf)
    table = LayoutObject(
        "table-object",
        ObjectKind.TABLE,
        (15.0, 15.0, 225.0, 125.0),
        tuple(span.span_id for span in page.text.spans),
        "Model-proposed table region",
        Confidence(None, "layout inference pending"),
    )
    with pytest.raises(ValueError, match="source SHA-256"):
        PdfspineTableAdapter().extract(b"not a PDF", page=page, item=table)
    text = LayoutObject(
        "text-object",
        ObjectKind.TEXT,
        table.bbox,
        table.source_span_ids,
        "Not a table",
        Confidence(None, "layout inference pending"),
    )
    with pytest.raises(ValueError, match="requires a Table"):
        PdfspineTableAdapter().extract(pdf, page=page, item=text)


def test_real_p20_sensitivity_region_reports_native_grid_unavailable() -> None:
    if not SAMPLE.is_file():
        pytest.skip(
            "Optional real AIA corpus is absent; provision the pinned local PDF to run source acceptance. No download is performed."
        )
    pdf = SAMPLE.read_bytes()
    assert sha256(pdf).hexdigest() == EXPECTED_SHA256
    extracted = PdfspineDocumentAdapter().extract_region(
        pdf, page_index=19, bbox=(0.0, 0.0, 960.0, 540.0)
    )
    svg = extracted.native_svg.encode()
    page = PageInput(
        "a" * 64,
        EXPECTED_SHA256,
        19,
        extracted.width,
        extracted.height,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", EXPECTED_SHA256, 19, extracted.text_spans),
    )
    spans = tuple(
        span.span_id
        for span in page.text.spans
        if (
            P20_SENSITIVITY_CANDIDATE[0]
            <= (span.bbox[0] + span.bbox[2]) / 2
            <= P20_SENSITIVITY_CANDIDATE[2]
            and P20_SENSITIVITY_CANDIDATE[1]
            <= (span.bbox[1] + span.bbox[3]) / 2
            <= P20_SENSITIVITY_CANDIDATE[3]
        )
    )
    item = LayoutObject(
        "p20-sensitivity-candidate",
        ObjectKind.TABLE,
        P20_SENSITIVITY_CANDIDATE,
        spans,
        "Provisional sensitivity matrix region from source review",
        Confidence(None, "provisional visual region; native table not established"),
    )

    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is None
    assert result.diagnostics == (
        f"pdfspine/{pdfspine.__version__} native lines found 1 page table(s) and 0 exact region match(es); typed table unavailable.",
    )
