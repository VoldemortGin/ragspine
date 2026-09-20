"""pdfspine typed slots are the only native table structure source."""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.pdfspine_tables import (
    PdfspineTableAdapter,
    fill_rectangles,
    ruling_segments,
)
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.geometry import Axis, Segment
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.table_models import (
    CellContentState,
    HeaderEvidenceKind,
    HeaderStrength,
    MergeProof,
    SlotState,
    TableIR,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    FILL_HEADER_TABLE,
    FRAME_ONLY_TABLE,
    MULTI_HEADER_TABLE,
    SPLIT_TABLE,
    UNRULED_TABLE,
    TableSpec,
    authored_pdf,
)

SAMPLE = Path("data/samples/aia-group-2026-interim-results-presentation.pdf")
EXPECTED_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
P20_SENSITIVITY_CANDIDATE = (654.0, 125.0, 934.0, 466.0)
# A published v2 ingestion snapshot whose page 2 holds a fully ruled 4x2 table: its stored
# ir.json predates ADR 0014 and must stay pending, while re-extracting the same region from
# the same source PDF must prove the grid and keep every cell id.
INGESTION_ROOT = Path(
    "data/ingestion/3f7233e3a7e40ad75f9579740b89bf7d88087528f3f9760fcd7b576d24c71813"
)
INGESTION_TABLE_REGION = (19.5, 119.8, 300.5, 224.3)
INGESTION_TABLE_OBJECT_ID = (
    "layout-object-v1:e8081a0a33af593215fc1e4bb50c7f7c00d4648dde811255b0d9ce1038f061d0"
)
INGESTION_TABLE_OBJECT_DIR = "object-ba0576f0dd0a758eb2b1"


def _center_in(region: tuple[float, float, float, float], bbox: tuple[float, ...]) -> bool:
    return (
        region[0] <= (bbox[0] + bbox[2]) / 2 <= region[2]
        and region[1] <= (bbox[1] + bbox[3]) / 2 <= region[3]
    )


def _segment_bounds(segment: Segment) -> tuple[float, float, float, float]:
    if segment.axis is Axis.HORIZONTAL:
        return (segment.start, segment.position, segment.end, segment.position)
    return (segment.position, segment.start, segment.position, segment.end)


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


def _drawn_pdf(draw: Callable[[pdfspine.Page], None]) -> bytes:
    """A 300x180 page with two occurrences and whatever rulings ``draw`` paints."""
    document = pdfspine.open()
    try:
        page = document.new_page(width=300, height=180)
        draw(page)
        page.insert_text((70, 50), "Header", fontsize=12)
        page.insert_text((50, 100), "Left", fontsize=12)
        return document.tobytes()
    finally:
        document.close()


def _thin_rule_pdf() -> bytes:
    def draw(page: pdfspine.Page) -> None:
        for y in (20.0, 70.0, 120.0):
            page.draw_rect((20.0, y - 0.25, 220.0, y + 0.25), color=None, fill=(0, 0, 0), width=0)
        for x in (20.0, 120.0, 220.0):
            page.draw_rect((x - 0.25, 20.0, x + 0.25, 120.0), color=None, fill=(0, 0, 0), width=0)

    return _drawn_pdf(draw)


def _cell_rect_pdf() -> bytes:
    def draw(page: pdfspine.Page) -> None:
        for top, bottom in ((20.0, 70.0), (70.0, 120.0)):
            for left, right in ((20.0, 120.0), (120.0, 220.0)):
                page.draw_rect((left, top, right, bottom), width=1)

    return _drawn_pdf(draw)


def _snapped_pdf() -> bytes:
    """Two half-width rules 2pt apart; pdfspine's 3.0pt snapping averages them to y=31."""

    def draw(page: pdfspine.Page) -> None:
        page.draw_line((20, 30), (150, 30), width=1)
        page.draw_line((150, 32), (280, 32), width=1)
        for y in (90, 150):
            page.draw_line((20, y), (280, y), width=1)
        for x in (20, 150, 280):
            page.draw_line((x, 30), (x, 150), width=1)

    return _drawn_pdf(draw)


def _doubled_pdf() -> bytes:
    """A double top border; pdfspine averages the three strokes to y=30.666..."""

    def draw(page: pdfspine.Page) -> None:
        for y in (30, 32, 30):
            page.draw_line((20, y), (280, y), width=1)
        for y in (90, 150):
            page.draw_line((20, y), (280, y), width=1)
        for x in (20, 150, 280):
            page.draw_line((x, 30), (x, 150), width=1)

    return _drawn_pdf(draw)


def _region(pdf: bytes, bbox: tuple[float, float, float, float]) -> tuple[PageInput, LayoutObject]:
    page = _page_input(pdf)
    item = LayoutObject(
        "table-object",
        ObjectKind.TABLE,
        bbox,
        tuple(span.span_id for span in page.text.spans),
        "Model-proposed table region",
        Confidence(None, "layout inference pending"),
    )
    return page, item


def _authored_region(tmp_path: Path, spec: TableSpec) -> tuple[bytes, PageInput, LayoutObject]:
    pdf = authored_pdf(
        tmp_path / "authored.pdf", page_count=1, label="Authored", table_page=spec
    ).read_bytes()
    x0, y0, x1, y1 = spec.bbox
    page, item = _region(pdf, (x0 - 5.0, y0 - 5.0, x1 + 5.0, y1 + 5.0))
    return pdf, page, item


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
    assert table.verification is Verification.VERIFIED
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
        f"pdfspine/{pdfspine.__version__}" in diagnostic for diagnostic in result.diagnostics
    )
    assert any("source=native" in diagnostic for diagnostic in result.diagnostics)
    assert table.cells[0].border is not None
    assert table.cells[0].border.merge_proof == MergeProof((), (1,))
    assert all(cell.verification is Verification.VERIFIED for cell in table.cells)
    evidence = table.grid_evidence
    assert evidence is not None
    assert evidence.rows == (20.0, 70.0, 120.0)
    assert evidence.cols == (20.0, 120.0, 220.0)
    assert evidence.segment_count == 6
    assert "Grid structure proved" in result.diagnostics[-1]


def test_ruling_segments_use_top_left_like_spans() -> None:
    pdf = _table_pdf()
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        segments = ruling_segments(document.load_page(0))
        assert fill_rectangles(document.load_page(0)) == ()
    finally:
        document.close()

    assert len(segments) == 6
    assert {segment.position for segment in segments if segment.axis is Axis.HORIZONTAL} == {
        20.0,
        70.0,
        120.0,
    }
    assert {segment.position for segment in segments if segment.axis is Axis.VERTICAL} == {
        20.0,
        120.0,
        220.0,
    }
    assert all(segment.edge == "l" and segment.thickness == 1.0 for segment in segments)
    header = next(span for span in _page_input(pdf).text.spans if span.text.strip() == "Header")
    assert header.bbox[1] >= 20.0 and header.bbox[3] <= 70.0


def test_ruling_segments_accept_thin_filled_rectangles_and_stroked_rects() -> None:
    document = pdfspine.open(stream=_thin_rule_pdf(), filetype="pdf")
    try:
        thin = ruling_segments(document.load_page(0))
    finally:
        document.close()
    assert len(thin) == 6
    assert {segment.edge for segment in thin} == {"re-thin"}
    assert {segment.position for segment in thin if segment.axis is Axis.HORIZONTAL} == {
        20.0,
        70.0,
        120.0,
    }
    assert {segment.position for segment in thin if segment.axis is Axis.VERTICAL} == {
        20.0,
        120.0,
        220.0,
    }
    assert {segment.thickness for segment in thin} == {0.5}

    pdf = _cell_rect_pdf()
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        stroked = ruling_segments(document.load_page(0))
    finally:
        document.close()
    assert len(stroked) == 16
    assert {segment.edge for segment in stroked} == {
        "re-top",
        "re-bottom",
        "re-left",
        "re-right",
    }
    page, item = _region(pdf, (15.0, 15.0, 225.0, 125.0))
    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)
    assert result.table is not None
    assert result.table.verification is Verification.VERIFIED


def test_snapped_or_doubled_boundaries_stay_pending() -> None:
    for pdf, expected in (
        (_snapped_pdf(), "row boundary 0 at y=31.0"),
        (_doubled_pdf(), "row boundary 0 at y=30.666"),
    ):
        page, item = _region(pdf, (15.0, 25.0, 285.0, 155.0))
        result = PdfspineTableAdapter().extract(pdf, page=page, item=item)
        assert result.table is not None
        assert result.table.verification is Verification.PENDING
        assert result.table.grid_evidence is None
        assert all(cell.border is None for cell in result.table.cells)
        assert result.diagnostics[-1].startswith("Grid structure pending: ")
        assert expected in result.diagnostics[-1]


def test_frame_only_and_unruled_tables_report_no_grid(tmp_path: Path) -> None:
    for spec in (FRAME_ONLY_TABLE, UNRULED_TABLE):
        pdf, page, item = _authored_region(tmp_path, spec)
        result = PdfspineTableAdapter().extract(pdf, page=page, item=item)
        assert result.table is None
        assert result.diagnostics == (
            f"pdfspine/{pdfspine.__version__} native lines found 0 page table(s) and 0 exact region match(es); typed table unavailable.",
        )


def test_fill_header_band_is_proved_from_filled_rectangles(tmp_path: Path) -> None:
    pdf, page, item = _authored_region(tmp_path, FILL_HEADER_TABLE)
    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is not None
    assert result.table.verification is Verification.VERIFIED
    evidence = result.table.grid_evidence
    assert evidence is not None
    (fill,) = tuple(header for header in evidence.headers if header.kind is HeaderEvidenceKind.FILL)
    assert (fill.strength, fill.rows, fill.fills) == (
        HeaderStrength.PROVED,
        (0,),
        ((20.0, 60.0, 220.0, 86.0),),
    )
    assert evidence.proved_header_rows() == frozenset({0})


def test_multi_header_table_proves_merges_and_thick_header(tmp_path: Path) -> None:
    pdf, page, item = _authored_region(tmp_path, MULTI_HEADER_TABLE)
    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is not None
    table = result.table
    assert table.verification is Verification.VERIFIED
    assert (table.row_count, table.col_count) == (4, 3)
    evidence = table.grid_evidence
    assert evidence is not None
    assert evidence.proved_header_rows() == frozenset({0, 1})
    assert evidence.proved_header_cols() == frozenset()
    unit = next(cell for cell in table.cells if (cell.row, cell.col) == (2, 2))
    assert unit.row_span == 2
    assert unit.border is not None
    assert unit.border.merge_proof == MergeProof((3,), ())
    group = next(cell for cell in table.cells if (cell.row, cell.col) == (0, 0))
    assert group.col_span == 2
    assert group.border is not None
    assert group.border.merge_proof == MergeProof((), (1,))


def test_split_segments_are_stitched(tmp_path: Path) -> None:
    pdf, page, item = _authored_region(tmp_path, SPLIT_TABLE)
    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is not None
    table = result.table
    assert table.verification is Verification.VERIFIED
    assert table.grid_evidence is not None
    assert table.grid_evidence.segment_count == 17
    assert all(cell.border is not None and len(cell.border.top) == 1 for cell in table.cells)


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

    # Read-only diagnosis of *why* the region has no native grid, printed under ``-s``.
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        source_page = document.load_page(19)
        segments = ruling_segments(source_page)
        inside = [
            segment
            for segment in segments
            if _center_in(P20_SENSITIVITY_CANDIDATE, _segment_bounds(segment))
        ]
        found = tuple(tuple(table.bbox) for table in source_page.find_tables(strategy="lines"))
        fills = len(fill_rectangles(source_page))
    finally:
        document.close()
    # A deliberate diagnostic for `pytest -s`: this real page is the reference case for
    # "detected as no table at all", and the counts are what the handoff records.
    print(  # noqa: T201
        f"p20 diagnosis: page_rulings={len(segments)} rulings_in_region={len(inside)} "
        f"fills={fills} found_table_bboxes={found}"
    )


def test_synthetic_ingestion_table_reproves_verified() -> None:
    source = INGESTION_ROOT / "source" / "source.pdf"
    if not source.is_file():
        pytest.skip(
            "Optional local ingestion store is absent; provision data/ingestion to run this snapshot check. No download is performed."
        )
    pdf = source.read_bytes()
    page = _page_input(pdf, page_index=2)
    spans = tuple(
        span.span_id for span in page.text.spans if _center_in(INGESTION_TABLE_REGION, span.bbox)
    )
    item = LayoutObject(
        INGESTION_TABLE_OBJECT_ID,
        ObjectKind.TABLE,
        INGESTION_TABLE_REGION,
        spans,
        "Four-row, two-column table with headers Metric and Value.",
        Confidence(None, "uncalibrated model layout inference"),
    )

    result = PdfspineTableAdapter().extract(pdf, page=page, item=item)

    assert result.table is not None
    table = result.table
    assert table.verification is Verification.VERIFIED
    evidence = table.grid_evidence
    assert evidence is not None
    assert evidence.rows == (120.0, 146.0, 172.0, 198.0, 224.0)
    assert evidence.cols == (20.0, 150.0, 300.0)
    assert evidence.segment_count == 8
    # Every rule is 1pt and no band is filled, so the header row is a hint, never a proof.
    assert evidence.proved_header_rows() == frozenset()
    assert {header.strength for header in evidence.headers} == {HeaderStrength.HEURISTIC}

    run = (INGESTION_ROOT / "processing" / "current-processing").read_text(encoding="utf-8").strip()
    stored_ir = (
        INGESTION_ROOT
        / "processing"
        / "runs"
        / run
        / "page-003"
        / "objects"
        / INGESTION_TABLE_OBJECT_DIR
        / "ir.json"
    )
    stored = TypeAdapter(TableIR).validate_json(stored_ir.read_bytes())

    assert stored.verification is Verification.PENDING
    assert stored.grid_evidence is None
    assert all(cell.border is None for cell in stored.cells)
    assert {cell.cell_id for cell in stored.cells} == {cell.cell_id for cell in table.cells}
