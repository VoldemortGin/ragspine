"""Overlay of features merged together: the ONNX layout (ADR 0030) with the index-text layout
(ADR 0027 / 0028 Amendment 1), the sharded store (ADR 0029) and the retrieval test bench.

The synthetic report of ``index_layout_helpers`` gets one grey graphic on every page (a little
narrower on each page, so it never reads as a repeated decoration), so the deterministic triage
declines every page and the stub ONNX layout partitions all of them:
a running header and page number as ``abandon`` blocks, the statement as a ``table`` block,
the rest as ``plain text``, the graphic as an ``image``. Offline, no real model.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pdfspine
import pytest
from pdfspine.geometry import Rect

import enterprise_pdf_rag.adapters.onnx_partition as onnx_partition
from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.onnx_partition import (
    ONNX_LAYOUT_MODEL_FILE,
    ONNX_MODELS_ENV,
    ONNX_PRODUCER_PREFIX,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.retrieval_testbench import run_retrieval_testbench
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput
from tests.enterprise_pdf_rag.adapters.index_layout_helpers import (
    ANSWER,
    FOOTER_LIMIT,
    HEADER_LIMIT,
    PAGES,
    QUESTION,
    STATEMENT_BBOX,
    STATEMENT_ROWS,
    STATEMENT_TITLE,
    report_env,
    report_pdf,
    run_report,
)
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import sharded_layout_only


def _graphic(page_index: int) -> tuple[float, float, float, float]:
    """Below the statement's rows and above the page number: no span, no other block."""
    return (300.0, 524.0, 400.0 - 3 * page_index, 576.0)


_RUNNING_OBJECTS = 2 * PAGES
_OFF = {"table_row_index_units": False, "drop_running_lines_from_index": False}

Bounds = tuple[float, float, float, float]


def _graphic_report(path: Path) -> Path:
    report_pdf(path)
    with pdfspine.open(stream=path.read_bytes()) as document:
        for index in range(PAGES):
            document.load_page(index).draw_rect(
                _graphic(index), color=None, fill=(0.6, 0.6, 0.6), width=0
            )
        path.write_bytes(document.tobytes())
    return path


def _center_inside(bbox: Bounds, region: Bounds) -> bool:
    x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


_LABELS = {
    "header": ("abandon", "header"),
    "footer": ("abandon", "number"),
    "table": ("table", "table"),
    "body": ("plain text", "text"),
}


def _stub_blocks(
    document: pdfspine.Document, page: PageInput, options: object
) -> list[pdfspine.LayoutBlock]:
    spans = page.text.spans
    statement = any(span.text == STATEMENT_TITLE for span in spans)
    groups: dict[str, list[Bounds]] = {}
    for span in spans:
        bbox = span.bbox
        name = (
            "header"
            if bbox[3] <= HEADER_LIMIT
            else "footer"
            if bbox[1] >= FOOTER_LIMIT
            else "table"
            if statement and _center_inside(bbox, STATEMENT_BBOX)
            else "body"
        )
        groups.setdefault(name, []).append(bbox)
    blocks = [
        pdfspine.LayoutBlock(
            Rect(
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ),
            _LABELS[name][0],
            0.95,
            _LABELS[name][1],
        )
        for name, boxes in groups.items()
    ]
    blocks.append(pdfspine.LayoutBlock(Rect(*_graphic(page.page_index)), "figure", 0.95, "image"))
    return blocks


def _onnx_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    (models / ONNX_LAYOUT_MODEL_FILE).write_bytes(b"stub-weights")
    monkeypatch.setenv(ONNX_MODELS_ENV, str(models))
    monkeypatch.setattr(onnx_partition, "layout_blocks", _stub_blocks)


def _layout_counts(result: FolderPipelineResult) -> tuple[int, int, int]:
    """Deterministic / ONNX / model-fallback pages, re-derived from the saved partitions."""
    (document,) = result.documents
    assert document.ingestion is not None
    ingested = document.ingestion
    return (
        ingested.pages_partitioned_deterministically,
        ingested.pages_partitioned_onnx,
        ingested.pages_partition_model_fallback,
    )


def _table_member_id(tmp_path: Path) -> str:
    (entry,) = scan_catalog(tmp_path / "ingestion").documents
    members = mount_document(entry, embedder=OfflineDescriptionEmbedder()).member_texts()
    (table,) = (member for member in members if member.kind is ObjectKind.TABLE)
    assert table.units is not None and len(table.units) == len(STATEMENT_ROWS)
    return table.member_id


def test_onnx_tables_get_row_units_and_onnx_running_lines_score_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = report_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    _graphic_report(tmp_path / "pdfs" / "report.pdf")

    result, prompts = run_report(
        tmp_path, answers="answers-onnx", ingest_mode="lite", layout_policy="onnx-layout"
    )

    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    assert _layout_counts(result) == (0, PAGES, 0)
    assert tasks["page-layout"] == 0 and document.live_calls == 0
    manifest = ProcessingStore(Path(document.ingestion.processing_store)).load(
        document.ingestion.processing_id
    )
    assert all(ONNX_PRODUCER_PREFIX in page.partition.producer for page in manifest.pages)
    # ADR 0027 Amendment 1: the ONNX Table (no grid) is indexed by rows, one unit per row.
    # ADR 0028 Amendment 1: the ONNX header / page-number Text objects score nothing.
    assert document.index is not None
    assert (document.index.row_unit_tables, document.index.row_units) == (1, len(STATEMENT_ROWS))
    assert document.index.unscored_running_members == _RUNNING_OBJECTS
    assert result.eval is not None
    (case,) = result.eval.cases
    assert (case.verdict, case.answer, case.cited_pages) == ("answered", ANSWER, (PAGES,))
    assert "fragments.row-" in prompts[-1]
    _table_member_id(tmp_path)


def test_switching_between_model_and_onnx_layouts_on_the_sharded_store_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = report_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    _graphic_report(tmp_path / "pdfs" / "report.pdf")

    model, _ = run_report(tmp_path, answers="answers-model", ingest_mode="lite")
    assert tasks["page-layout"] == PAGES
    onnx, _ = run_report(
        tmp_path, answers="answers-onnx", ingest_mode="lite", layout_policy="onnx-layout"
    )
    assert tasks["page-layout"] == PAGES and onnx.documents[0].live_calls == 0
    assert (_layout_counts(model), _layout_counts(onnx)) == ((0, 0, 0), (0, PAGES, 0))
    # Both layouts' artifacts sit side by side in the sharded (ADR 0029) directories.
    assert sharded_layout_only(tmp_path / "ingestion")
    for policy, counts in (("model", (0, 0, 0)), ("onnx-layout", (0, PAGES, 0))):
        again, _ = run_report(
            tmp_path, answers=f"answers-{policy}-again", ingest_mode="lite", layout_policy=policy
        )
        (document,) = again.documents
        assert (document.live_calls, document.storage_repairs) == (0, {})
        assert _layout_counts(again) == counts
    assert tasks["page-layout"] == PAGES
    assert sharded_layout_only(tmp_path / "ingestion")


def _ranked(audit: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(audit)) as connection:
        (raw,) = connection.execute(
            "SELECT ranked FROM answers ORDER BY id DESC LIMIT 1"
        ).fetchone()
    ranked: list[dict[str, object]] = json.loads(raw)
    return ranked


def test_the_bench_reads_unit_scored_members_with_their_pages_and_diagnoses_lite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    _graphic_report(tmp_path / "pdfs" / "report.pdf")
    # The run's own question set carries no pages; the bench's copy names the statement page.
    questions = tmp_path / "bench.jsonl"
    questions.write_text(
        json.dumps(
            {
                "id": "q1",
                "question": QUESTION,
                "doc": "report.pdf",
                "pages": PAGES,
                "expected": ANSWER,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    audit = tmp_path / "ingestion" / "answers-audit.sqlite"

    def bench(result: FolderPipelineResult) -> dict[str, object]:
        (row,) = run_retrieval_testbench(
            audit, questions, report=result, ingestion_root=tmp_path / "ingestion"
        ).rows
        return {
            "ranking": row.ranking,
            "fused_page_rank": row.fused_page_rank,
            "in_prompt": row.in_prompt,
            "diagnosis": row.diagnosis,
        }

    # Row units on (lite): the statement member, scored by its row units, ranks first and its
    # page in the journalled ranking is still the statement page.
    on, _ = run_report(
        tmp_path, answers="answers-on", ingest_mode="lite", layout_policy="onnx-layout"
    )
    table = _table_member_id(tmp_path)
    seats = [entry for entry in _ranked(audit) if entry["member_id"] == table]
    assert len(seats) == 1 and seats[0]["page_index"] == PAGES - 1
    assert _ranked(audit)[0]["member_id"] == table
    assert bench(on) == {
        "ranking": "full",
        "fused_page_rank": 1,
        "in_prompt": True,
        "diagnosis": "correct",
    }

    # Row units off: the whole statement is one unit, crowded out of the prompt; the bench
    # says the ranking had it but the prompt did not.
    off, _ = run_report(
        tmp_path, answers="answers-off", ingest_mode="lite", layout_policy="onnx-layout", **_OFF
    )
    assert off.eval is not None and off.eval.cases[0].verdict == "abstained"
    result = bench(off)
    assert result["ranking"] == "full" and result["diagnosis"] == "retrieved_not_in_prompt"
