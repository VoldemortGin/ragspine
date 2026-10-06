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
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pdfspine
import pytest
from pdfspine.geometry import Rect

import enterprise_pdf_rag.adapters.onnx_partition as onnx_partition
from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.onnx_partition import (
    ONNX_LAYOUT_MODEL_FILE,
    ONNX_MODELS_ENV,
    ONNX_PRODUCER_PREFIX,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.retrieval_testbench import run_retrieval_testbench
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from ragspine.extraction.evidence.objects.tables.table_inferred_grid import TSR_PRODUCER
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput
from ragspine.extraction.tables.structure import CellBox, TableRegion, TableStructure
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
    write_question,
)
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import sharded_layout_only
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client


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


# ---- ADR 0031 (TSR pending grid) on top: ONNX tables, row units, the bench -----------------

# Column boundaries the stub structure model draws through the statement: the stub column
# (row labels and ``US$m``), then the 2024 and 2023 figure columns.
_FIRST_FIGURE_COLUMN = 290.0
_SECOND_FIGURE_COLUMN = 350.0


@dataclass
class _WordGridModel:
    """A deterministic stand-in for SLANet-plus: one row per printed line, three columns; the
    first line (the period caption) spans both figure columns. ``None`` when ``refuse``."""

    refuse: bool = False
    name: str = "stub"
    producer: str = f"{TSR_PRODUCER}:pdfspine/{pdfspine.__version__}:stub00000000"
    regions: list[TableRegion] = field(default_factory=list)

    def recognize(self, region: TableRegion) -> TableStructure | None:
        self.regions.append(region)
        if self.refuse:
            return None
        lines = sorted({round(word.center[1]) for word in region.words})
        edges = [region.bbox[1]]
        edges.extend((upper + lower) / 2 for upper, lower in pairwise(lines))
        edges.append(region.bbox[3])
        xs = (region.bbox[0], _FIRST_FIGURE_COLUMN, _SECOND_FIGURE_COLUMN, region.bbox[2])
        cells = [
            CellBox(0, 0, 1, 1, (xs[0], edges[0], xs[1], edges[1])),
            CellBox(0, 1, 1, 2, (xs[1], edges[0], xs[3], edges[1])),
        ]
        cells.extend(
            CellBox(row, col, 1, 1, (xs[col], edges[row], xs[col + 1], edges[row + 1]))
            for row in range(1, len(lines))
            for col in range(3)
        )
        return TableStructure(n_rows=len(lines), n_cols=3, cells=tuple(cells))


def _cell_script(prompt: str) -> ModelAnswer:
    """Answer only from an inferred cell on the asked row under the 2024 column."""
    for block in prompt.split("| member ")[1:]:
        for line in block.splitlines()[1:]:
            if (
                line.startswith("cells.")
                and 'inferred_row="Insurance revenue"' in line
                and "2024" in line.split("inferred_col=", 1)[-1]
            ):
                path, text = line.split(" ", 1)[0], line.split(": ", 1)[1].split(" inferred_")[0]
                claim = ModelClaim(
                    claim_id="c1", member_id=block[:64], kind="cell", field_path=path, text=text
                )
                return answered(text, claim)
    return declined()


def _tsr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: _WordGridModel, answers: str
) -> tuple[FolderPipelineResult, list[str]]:
    monkeypatch.setattr(pdfspine_tsr, "table_structure_recognizer", lambda *_: model)
    llm, prompts = scripted_client(tmp_path / answers, _cell_script, max_live_calls=5)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        questions=write_question(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        build_tree=False,
        ingest_mode="lite",
        layout_policy="onnx-layout",
        unverified_table_structure="tsr",
    )
    return result, prompts


def test_an_onnx_table_gets_a_pending_tsr_grid_and_is_answered_from_a_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = report_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    _graphic_report(tmp_path / "pdfs" / "report.pdf")
    model = _WordGridModel()

    result, prompts = _tsr_run(tmp_path, monkeypatch, model, "answers-tsr")

    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    assert _layout_counts(result) == (0, PAGES, 0) and tasks["page-layout"] == 0
    # The ONNX-proposed Table had no ruled grid: the structure model ran on it, and its grid
    # passed the self-check, so it is not indexed as verbatim rows.
    assert model.regions
    ingested = document.ingestion
    assert (ingested.table_tsr_grids, ingested.table_tsr_fallbacks) == (1, 0)
    assert ingested.table_row_transcriptions == 0
    assert document.index is not None
    # ADR 0027 Amendment 1 now splits the pending grid too (one unit per figure row, its
    # inferred header rows repeated); as one unit it lost its seat to the narrative pages.
    assert (document.index.row_unit_tables, document.index.row_units) == (1, len(STATEMENT_ROWS))
    assert document.index.unscored_running_members == _RUNNING_OBJECTS
    (entry,) = scan_catalog(tmp_path / "ingestion").documents
    members = mount_document(entry, embedder=OfflineDescriptionEmbedder()).member_texts()
    (table,) = (member for member in members if member.kind is ObjectKind.TABLE)
    assert table.units is not None and len(table.units) == len(STATEMENT_ROWS)
    revenue = next(unit for unit in table.units if "Insurance revenue" in unit)
    assert revenue.endswith(
        "For the six months ended 30 June\nUS$m\t2024\t2023\nInsurance revenue\t12,345\t9,016"
    )
    assert result.eval is not None
    (case,) = result.eval.cases
    assert (case.verdict, case.answer, case.cited_pages) == ("answered", ANSWER, (PAGES,))
    assert "grid=inferred" in prompts[-1]

    # The bench maps the TSR member to its page in the journalled ranking.
    audit = tmp_path / "ingestion" / "answers-audit.sqlite"
    seats = [entry for entry in _ranked(audit) if entry["member_id"] == table.member_id]
    assert len(seats) == 1 and seats[0]["page_index"] == PAGES - 1
    questions = tmp_path / "bench.jsonl"
    record = {
        "id": "q1",
        "question": QUESTION,
        "doc": "report.pdf",
        "pages": PAGES,
        "expected": ANSWER,
    }
    questions.write_text(json.dumps(record) + "\n", encoding="utf-8")
    (row,) = run_retrieval_testbench(
        audit, questions, report=result, ingestion_root=tmp_path / "ingestion"
    ).rows
    assert (row.ranking, row.in_prompt, row.diagnosis) == ("full", True, "correct")


def test_an_onnx_table_whose_tsr_grid_is_refused_falls_back_to_row_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_env(monkeypatch)
    _onnx_env(tmp_path, monkeypatch)
    _graphic_report(tmp_path / "pdfs" / "report.pdf")

    result, _ = _tsr_run(tmp_path, monkeypatch, _WordGridModel(refuse=True), "answers-refused")

    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    ingested = document.ingestion
    assert (ingested.table_tsr_grids, ingested.table_tsr_fallbacks) == (0, 1)
    assert ingested.table_tsr_fallback_reasons == {"no_structure": 1}
    assert ingested.table_row_transcriptions == 1
    assert document.index is not None
    assert (document.index.row_unit_tables, document.index.row_units) == (1, len(STATEMENT_ROWS))
