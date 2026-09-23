"""Page text-layer diagnostics on authored PDFs: outlined text and garbled spans are named.

Every fixture is authored in the test with pdfspine; nothing binary is committed. A font
without ToUnicode cannot be authored with pdfspine's base fonts, so the garbled page adds
its undecodable span at the ``get_text("dict")`` seam every source reader shares.
"""

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.draft_publication import (
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.http.document_schemas import ManifestEnvelope
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.pdf_ingestion import ingest_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import (
    _WARNINGS,
    PdfspineDocumentAdapter,
    _observed_spans,
    _PageText,
)
from enterprise_pdf_rag.adapters.source_objects import source_object_ir
from ragspine.extraction.evidence.document.models import (
    AssetRef,
    PageExtraction,
    TextLayerStatus,
    TextSidecar,
)
from ragspine.extraction.evidence.document.text_layer import (
    OUTLINED_TEXT_DRAWINGS_PER_CHAR,
    OUTLINED_TEXT_MIN_DRAWINGS,
)
from ragspine.extraction.evidence.figures.models import Confidence
from ragspine.extraction.evidence.page.models import LayoutObject, ObjectKind, PageInput
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    ingest_generic_semantics,
)

_BODY = ("Revenue grew 12% in the period", "Operating margin improved")
_CHART_TITLE = "Revenue by segment 2024"
_CHART_LABELS = ("Asia", "Europe", "Americas", "0", "50", "100")
# An ASCII marker inside the undecodable span, so a byte scan finds it however JSON escapes it.
_GARBLED_MARKER = "ZQXJ"
_GARBLED_TEXT = _GARBLED_MARKER + "\ue000\ue001\ufffd\ue002"
_GARBLED_BBOX = (20.0, 100.0, 90.0, 112.0)


def _text_page(document: pdfspine.Document) -> None:
    page = document.new_page(width=240, height=160)
    for line, text in enumerate(_BODY):
        page.insert_text((20, 40 + 16 * line), text, fontsize=10)


def _outlined_page(document: pdfspine.Document) -> None:
    """Three lines of filled glyph-like outlines and no text operator at all."""
    page = document.new_page(width=240, height=160)
    for line in range(3):
        for glyph in range(40):
            x, y = 12.0 + 5.4 * glyph, 30.0 + 18.0 * line
            page.draw_polyline(
                [(x, y), (x + 1.8, y - 7.0), (x + 3.6, y), (x + 1.8, y - 2.0)],
                color=(0, 0, 0),
                fill=(0, 0, 0),
                closePath=True,
            )


def _chart_page(document: pdfspine.Document) -> None:
    """A dense vector bar chart whose title and axis labels are real text spans."""
    page = document.new_page(width=240, height=160)
    page.insert_text((20, 18), _CHART_TITLE, fontsize=9)
    page.draw_line((20, 140), (230, 140), width=0.6)
    page.draw_line((20, 30), (20, 140), width=0.6)
    for bar in range(90):
        x = 22.0 + 2.3 * bar
        page.draw_rect(
            (x, 140.0 - (bar * 7) % 100 - 5, x + 1.6, 140.0), color=None, fill=(0.2, 0.4, 0.8)
        )
    for index, label in enumerate(_CHART_LABELS):
        page.insert_text((24 + 34 * index, 152), label, fontsize=7)


def _authored(tmp_path: Path, *pages: str) -> Path:
    authors = {"text": _text_page, "outlined": _outlined_page, "chart": _chart_page}
    path = tmp_path / "authored.pdf"
    with pdfspine.open() as document:
        for kind in pages:
            authors[kind](document)
        path.write_bytes(document.tobytes())
    return path


def _extract(tmp_path: Path, *pages: str) -> tuple[PageExtraction, ...]:
    return (
        PdfspineDocumentAdapter().extract_document(_authored(tmp_path, *pages).read_bytes()).pages
    )


def _garbled_block() -> dict[str, object]:
    span = {
        "text": _GARBLED_TEXT,
        "bbox": _GARBLED_BBOX,
        "origin": (_GARBLED_BBOX[0], _GARBLED_BBOX[3] - 2.0),
        "font": "NoToUnicode",
        "size": 10.0,
        "flags": 0,
        "color": 0,
    }
    return {
        "type": 0,
        "bbox": _GARBLED_BBOX,
        "lines": [{"dir": (1.0, 0.0), "wmode": 0, "bbox": _GARBLED_BBOX, "spans": [span]}],
    }


@pytest.fixture
def garbled_first_page(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Page 1's text dictionary also reports one span decoded without ToUnicode."""
    original = pdfspine.Page.get_text

    # Every source reader calls ``get_text`` with one positional option and nothing else.
    def get_text(page: pdfspine.Page, option: str = "text") -> object:
        result = original(page, option)
        if option == "dict" and page.number == 0 and isinstance(result, dict):
            result = {**result, "blocks": [*result["blocks"], _garbled_block()]}
        return result

    monkeypatch.setattr(pdfspine.Page, "get_text", get_text)
    yield


def test_page_of_vector_glyph_outlines_without_a_text_layer_is_outlined_text(
    tmp_path: Path,
) -> None:
    (page,) = _extract(tmp_path, "outlined")

    assert page.text_spans == ()
    assert page.text_layer is not None
    assert page.text_layer.status is TextLayerStatus.OUTLINED_TEXT
    assert page.text_layer.needs_ocr is True
    assert (page.text_layer.span_count, page.text_layer.char_count) == (0, 0)
    assert page.text_layer.drawing_count >= OUTLINED_TEXT_MIN_DRAWINGS
    assert page.warnings[: len(_WARNINGS)] == _WARNINGS
    (warning,) = page.warnings[len(_WARNINGS) :]
    assert "outlined_text" in warning and "OCR" in warning


def test_normal_text_page_is_ok_and_keeps_its_spans_and_warnings(tmp_path: Path) -> None:
    (page,) = _extract(tmp_path, "text")

    assert tuple(span.text for span in page.text_spans) == _BODY
    assert page.warnings == _WARNINGS
    assert page.text_layer is not None
    assert page.text_layer.status is TextLayerStatus.OK
    assert page.text_layer.needs_ocr is False
    assert page.text_layer.span_count == len(_BODY)
    assert page.text_layer.char_count == sum(not c.isspace() for text in _BODY for c in text)
    assert (page.text_layer.garbled_char_count, page.text_layer.garbled_span_count) == (0, 0)


def test_dense_vector_chart_with_real_labels_is_not_outlined_text(tmp_path: Path) -> None:
    (page,) = _extract(tmp_path, "chart")

    assert page.text_layer is not None
    # The fixture must actually exercise the per-character rule, not only the path minimum.
    assert page.text_layer.drawing_count >= OUTLINED_TEXT_MIN_DRAWINGS
    assert page.text_layer.drawing_count < (
        page.text_layer.char_count * OUTLINED_TEXT_DRAWINGS_PER_CHAR
    )
    assert page.text_layer.status is TextLayerStatus.OK
    assert page.warnings == _WARNINGS
    painted = " ".join(span.text for span in page.text_spans)
    assert all(label in painted for label in (_CHART_TITLE, *_CHART_LABELS))


@pytest.mark.usefixtures("garbled_first_page")
def test_garbled_span_is_withheld_from_the_sidecar_and_the_page_is_garbled(
    tmp_path: Path,
) -> None:
    (page,) = _extract(tmp_path, "text")

    assert tuple(span.text for span in page.text_spans) == _BODY
    assert page.text_layer is not None
    assert page.text_layer.status is TextLayerStatus.GARBLED
    assert page.text_layer.span_count == len(_BODY) + 1
    assert page.text_layer.garbled_span_count == 1
    assert page.text_layer.garbled_char_count == 4
    assert any("garbled" in warning and "OCR" in warning for warning in page.warnings)


@pytest.mark.usefixtures("garbled_first_page")
def test_a_layout_object_citing_the_garbled_span_cannot_be_certified(tmp_path: Path) -> None:
    pdf = _authored(tmp_path, "text").read_bytes()
    (page,) = PdfspineDocumentAdapter().extract_document(pdf).pages
    digest = sha256(pdf).hexdigest()
    with pdfspine.open(stream=pdf, filetype="pdf") as document:
        observed = _observed_spans(
            _PageText.model_validate(document.load_page(0).get_text("dict")),
            source_digest=digest,
            page_index=0,
        )
    (garbled_id,) = (span.span_id for span in observed if span.text == _GARBLED_TEXT)
    assert garbled_id not in {span.span_id for span in page.text_spans}
    source = PageInput(
        "a" * 64,
        digest,
        0,
        page.width,
        page.height,
        AssetRef(sha256(b"svg").hexdigest(), "image/svg+xml", 3),
        TextSidecar("text-spans-v1", digest, 0, page.text_spans),
    )
    item = LayoutObject(
        "body",
        ObjectKind.TEXT,
        (0.0, 0.0, page.width, page.height),
        (page.text_spans[0].span_id, garbled_id),
        "Body text",
        Confidence(None, "test inference"),
    )

    with pytest.raises(ValueError, match="unbound source occurrence"):
        source_object_ir(source, item)


@pytest.mark.usefixtures("garbled_first_page")
def test_garbled_span_never_reaches_any_stored_artifact_or_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest, _ = ingest_generic_semantics(tmp_path, monkeypatch)

    assert ingest.ocr_needed_pages == (1,)
    assert ingest.text_layer_page_states == {"garbled": 1, "ok": 2}
    source_store, processing_store = Path(ingest.source_store), Path(ingest.processing_store)
    qualify_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
    )
    indexed = index_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=ingest.processing_id,
        embedder=OfflineDescriptionEmbedder(),
    )
    assert indexed.member_count >= 1
    publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )

    stored = [path for path in (tmp_path / "ingestion").rglob("*") if path.is_file()]
    assert stored
    assert not [path for path in stored if _GARBLED_MARKER.encode() in path.read_bytes()]


def test_ingestion_summary_names_the_pages_that_need_ocr(tmp_path: Path) -> None:
    summary = ingest_pdf(
        pdf=_authored(tmp_path, "text", "outlined", "chart"), output_dir=tmp_path / "out"
    )

    assert summary.ocr_needed_pages == (2,)
    assert summary.text_layer_page_states == {"ok": 2, "outlined_text": 1}


def test_manifest_written_before_text_layer_diagnostics_still_loads(tmp_path: Path) -> None:
    summary = ingest_pdf(pdf=_authored(tmp_path, "text"), output_dir=tmp_path / "out")
    manifest_path = Path(summary.source_store) / "objects" / "sha256" / summary.source_manifest_id
    payload = json.loads(manifest_path.read_bytes())
    for page in payload["manifest"]["pages"]:
        assert page.pop("text_layer")["status"] == "ok"

    legacy = ManifestEnvelope.model_validate_json(json.dumps(payload)).manifest

    assert all(page.text_layer is None for page in legacy.pages)
