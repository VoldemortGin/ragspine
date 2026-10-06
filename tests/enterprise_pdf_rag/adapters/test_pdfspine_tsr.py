"""The real SLANet-plus model on the synthetic statement (ADR 0031); skipped without its weights.

Set ``PDFSPINE_ONNX_MODELS`` to the directory holding ``slanet-plus.onnx`` and install
``pdfspine[onnx]`` to run it. Synthetic page only; nothing here reads ``data/``.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.pdf_password import open_pdf
from ragspine.extraction.evidence.document.models import TextSpan
from ragspine.extraction.evidence.figures.models import SourceAnchor
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import (
    TSR_PRODUCER,
    InferredGridRejection,
    inferred_header_rows,
    structure_producer,
)
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from tests.enterprise_pdf_rag.adapters.table_rows_helpers import STATEMENT_BBOX, statement_pdf


def _recognizer() -> pdfspine_tsr.SlanetPlusRecognizer:
    root = os.environ.get(pdfspine_tsr.MODELS_ENV)
    if not root or not (Path(root) / pdfspine_tsr.MODEL_FILE).is_file():
        pytest.skip("SLANet-plus weights are not configured (PDFSPINE_ONNX_MODELS)")
    pytest.importorskip("onnxruntime")
    return pdfspine_tsr.table_structure_recognizer()


def test_slanet_plus_infers_the_unruled_statement_and_reproduces_it(tmp_path: Path) -> None:
    recognizer = _recognizer()
    assert recognizer.producer.startswith(f"{TSR_PRODUCER}:pdfspine/")
    pdf = statement_pdf(tmp_path / "statement.pdf", page_count=1)
    with open_pdf(pdf.read_bytes()) as document:
        page = document.load_page(0)
        text: dict[str, Any] = page.get_text("dict")  # type: ignore[assignment]
        spans = tuple(
            TextSpan(f"s{index}", span["text"], tuple(span["bbox"]))
            for index, span in enumerate(
                span
                for block in text["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
                if STATEMENT_BBOX[1] <= span["bbox"][1] and span["bbox"][3] <= STATEMENT_BBOX[3]
            )
        )
        anchor = SourceAnchor("a" * 64, "a" * 64, 0, STATEMENT_BBOX)
        table = pdfspine_tsr.infer_table_grid(
            page, object_id="t", anchor=anchor, spans=spans, recognizer=recognizer
        )
        assert not isinstance(table, InferredGridRejection), table
        assert isinstance(table, TableIR)
        assert structure_producer(table) == recognizer.producer
        assert inferred_header_rows(table) >= 1
        by_slot = {(cell.row, cell.col): cell.text for cell in table.cells}
        revenue = next(
            row for (row, col), text in by_slot.items() if text == "Revenue" and col == 0
        )
        assert (by_slot[(revenue, 1)], by_slot[(revenue, 2)]) == ("1,234,567", "1,100,200")
        # Deterministic: the same page and weights give the same IR, so resolve can re-check it.
        pdfspine_tsr.recheck_inferred_table(page, table, spans)
