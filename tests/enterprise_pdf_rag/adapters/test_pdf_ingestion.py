"""Generic ingestion preserves source identity without model or publication effects."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.models import CanonicalPage, StageState
from ragspine.common.evidence.settings import ROOT_DIR
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage

# Ruled 3x2 grid drawn on the last page by ``authored_pdf(table_page=True)``; the
# bottom-right cell is left blank on purpose so cell states differ.
TABLE_COLUMNS = (20.0, 120.0, 220.0)
TABLE_ROWS = (60.0, 86.0, 113.0, 140.0)
TABLE_CELLS = {
    (0, 0): "Metric",
    (0, 1): "Value",
    (1, 0): "Revenue",
    (1, 1): "1,234",
    (2, 0): "Margin",
}


# Two stroked node frames joined by a straight connector whose filled triangle points at
# the second frame, drawn on the last page by ``authored_pdf(diagram_page=True)``.
DIAGRAM_NODES = {
    "n1": ((20.0, 70.0, 90.0, 100.0), "PLAN", (28.0, 89.0)),
    "n2": ((150.0, 70.0, 220.0, 100.0), "BUILD", (158.0, 89.0)),
}
DIAGRAM_LINE = ((90.0, 85.0), (142.0, 85.0))
DIAGRAM_ARROWHEAD = ((142.0, 81.0), (142.0, 89.0), (150.0, 85.0))
DIAGRAM_REGION = (15.0, 65.0, 225.0, 105.0)


# ``ROE = Net profit / Equity`` over a drawn fraction rule, plus a typographic ``x`` with a
# smaller raised ``2`` (pdfspine cannot write a real ``Ts``, so that superscript is derived).
# Both boxes are the layout regions the stub partitioner proposes; they hold the authored
# spans exactly as the embedded fixture font lays them out.
FORMULA_FRACTION_BBOX = (18.0, 56.0, 132.0, 100.0)
FORMULA_POWER_BBOX = (150.0, 62.0, 172.0, 86.0)
FORMULA_RULE = ((60.0, 78.0), (120.0, 78.0))


def _draw_formula(page: pdfspine.Page, fontname: str, *, rule: bool = True) -> None:
    page.insert_text((20, 82), "ROE =", fontsize=12, fontname=fontname)
    page.insert_text((62, 74), "Net profit", fontsize=11, fontname=fontname)
    if rule:
        page.draw_line(*FORMULA_RULE, width=0.8)
    page.insert_text((72, 94), "Equity", fontsize=11, fontname=fontname)
    page.insert_text((152, 82), "x", fontsize=12, fontname=fontname)
    page.insert_text((160, 76), "2", fontsize=7, fontname=fontname)


@dataclass(frozen=True)
class TableSpec:
    """A ruled grid to author: boundaries, cell texts, merges and header styling."""

    rows: tuple[float, ...] = TABLE_ROWS
    cols: tuple[float, ...] = TABLE_COLUMNS
    cells: Mapping[tuple[int, int], str] = field(default_factory=lambda: dict(TABLE_CELLS))
    merges: tuple[tuple[int, int, int, int], ...] = ()
    header_rows: int = 0
    header_rule_width: float | None = None
    line_width: float = 1.0
    ruled: bool = True
    frame_only: bool = False
    split_segments: bool = False
    fill_header: bool = False
    text_dy: float = 18.0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.cols[0], self.rows[0], self.cols[-1], self.rows[-1])


# Byte-identical to the grid ``table_page=True`` has always drawn: whole lines, rows
# before columns, width 1, text 18pt below each row boundary.
DEFAULT_TABLE = TableSpec()
# Two header rows closed by a 2pt rule, a column-spanning title and a row-spanning unit.
MULTI_HEADER_TABLE = TableSpec(
    rows=(48.0, 70.0, 92.0, 114.0, 136.0),
    cols=(20.0, 90.0, 160.0, 220.0),
    cells={
        (0, 0): "Group",
        (0, 2): "Unit",
        (1, 0): "Metric",
        (1, 1): "Value",
        (2, 0): "Revenue",
        (2, 1): "1,234",
        (2, 2): "m",
        (3, 0): "Margin",
        (3, 1): "12%",
    },
    merges=((0, 0, 1, 2), (2, 2, 2, 1)),
    header_rows=2,
    header_rule_width=2.0,
    text_dy=16.0,
)
FILL_HEADER_TABLE = TableSpec(header_rows=1, fill_header=True)
FRAME_ONLY_TABLE = TableSpec(frame_only=True)
UNRULED_TABLE = TableSpec(ruled=False)
SPLIT_TABLE = TableSpec(split_segments=True)


def _blocked(spec: TableSpec, *, boundary: int, index: int, horizontal: bool) -> bool:
    """Is the piece of boundary ``boundary`` in column/row ``index`` inside a merged cell?"""
    for row, col, row_span, col_span in spec.merges:
        if horizontal and row < boundary < row + row_span and col <= index < col + col_span:
            return True
        if not horizontal and col < boundary < col + col_span and row <= index < row + row_span:
            return True
    return False


def _runs(spec: TableSpec, *, boundary: int, count: int, horizontal: bool) -> list[tuple[int, int]]:
    """Maximal runs ``[start, end)`` of un-blocked pieces along one boundary."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(count + 1):
        open_piece = index < count and not _blocked(
            spec, boundary=boundary, index=index, horizontal=horizontal
        )
        if open_piece and start is None:
            start = index
        if not open_piece and start is not None:
            runs.append((start, index))
            start = None
    if spec.split_segments:
        return [(piece, piece + 1) for begin, end in runs for piece in range(begin, end)]
    return runs


def _draw_table(page: pdfspine.Page, fontname: str, spec: TableSpec = DEFAULT_TABLE) -> None:
    row_count, col_count = len(spec.rows) - 1, len(spec.cols) - 1
    if spec.ruled and spec.frame_only:
        page.draw_rect(spec.bbox, width=spec.line_width)
    elif spec.ruled:
        if spec.fill_header:
            for row in range(spec.header_rows):
                page.draw_rect(
                    (spec.cols[0], spec.rows[row], spec.cols[-1], spec.rows[row + 1]),
                    color=None,
                    fill=(0.85, 0.85, 0.85),
                    width=0,
                )
        for index, y in enumerate(spec.rows):
            width = (
                spec.header_rule_width
                if (spec.header_rule_width is not None and index == spec.header_rows)
                else spec.line_width
            )
            for start, end in _runs(spec, boundary=index, count=col_count, horizontal=True):
                page.draw_line((spec.cols[start], y), (spec.cols[end], y), width=width)
        for index, x in enumerate(spec.cols):
            for start, end in _runs(spec, boundary=index, count=row_count, horizontal=False):
                page.draw_line((x, spec.rows[start]), (x, spec.rows[end]), width=spec.line_width)
    for (row, column), text in spec.cells.items():
        page.insert_text(
            (spec.cols[column] + 6, spec.rows[row] + spec.text_dy),
            text,
            fontsize=11,
            fontname=fontname,
        )


def _draw_diagram(page: pdfspine.Page, fontname: str) -> None:
    for rect, label, origin in DIAGRAM_NODES.values():
        page.draw_rect(rect, color=(0, 0, 0), width=1)
        page.insert_text(origin, label, fontsize=10, fontname=fontname)
    page.draw_line(*DIAGRAM_LINE, width=1)
    # ``draw_polyline`` has no fill/closePath parameters; a filled triangle needs a Shape.
    shape = page.new_shape()
    shape.draw_polyline(list(DIAGRAM_ARROWHEAD))
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.5, closePath=True)
    shape.commit()


def authored_pdf(
    path: Path,
    *,
    page_count: int,
    label: str,
    embedded_font: bool = False,
    table_page: bool | TableSpec = False,
    diagram_page: bool = False,
    diagram_caption: bool = False,
    formula_page: bool = False,
    formula_rule: bool = True,
) -> Path:
    """``diagram_caption`` is carried for the partition stub that owns the caption line."""
    assert sum(bool(value) for value in (table_page, diagram_page, formula_page)) <= 1, (
        "Only one authored layout fits the last page"
    )
    assert diagram_page or not diagram_caption, "A diagram caption needs the diagram layout"
    spec = (
        table_page if isinstance(table_page, TableSpec) else (DEFAULT_TABLE if table_page else None)
    )
    with pdfspine.open() as document:
        for number in range(page_count):
            page = document.new_page(width=240, height=160)
            fontname = "helv"
            if embedded_font:
                fontname = "Authored"
                cast(_FontInsertionPage, page).insert_font(
                    fontname=fontname,
                    fontbuffer=(
                        ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf"
                    ).read_bytes(),
                )
            page.insert_text((20, 40), f"{label} page {number + 1}", fontsize=12, fontname=fontname)
            if spec is not None and number == page_count - 1:
                _draw_table(page, fontname, spec)
            if diagram_page and number == page_count - 1:
                _draw_diagram(page, fontname)
            if formula_page and number == page_count - 1:
                _draw_formula(page, fontname, rule=formula_rule)
        path.write_bytes(document.tobytes())
    return path


def test_authored_diagram_page_draws_native_shapes_and_two_label_spans(tmp_path: Path) -> None:
    pdf = authored_pdf(
        tmp_path / "diagram.pdf",
        page_count=1,
        label="Diagram",
        embedded_font=True,
        diagram_page=True,
    )
    with pdfspine.open(stream=pdf.read_bytes()) as document:
        page = document.load_page(0)
        svg = page.get_svg_image(text_as_path=False)
        extraction = cast(dict[str, Any], page.get_text("dict"))

    # The embedded font keeps every glyph a scaled <path>, so render_svg_png accepts the crop.
    assert "<text" not in svg
    geometry = [
        element for element in re.findall(r"<path[^>]*/>", svg) if "transform=" not in element
    ]
    assert len(geometry) >= 4
    assert 'd="M20 60L90 60L90 90L20 90Z"' in svg and 'd="M90 75L142 75"' in svg
    assert 'd="M142 79L142 71L150 75L142 79Z" fill="#000000"' in svg
    labels = {
        str(span["text"]): tuple(span["bbox"])
        for block in extraction["blocks"]
        for line in block["lines"]
        for span in line["spans"]
        if DIAGRAM_REGION[1] <= span["bbox"][1] and span["bbox"][3] <= DIAGRAM_REGION[3]
    }
    assert set(labels) == {"PLAN", "BUILD"}
    for node_id, (rect, label, _origin) in DIAGRAM_NODES.items():
        bbox = labels[label]
        assert rect[0] <= bbox[0] and bbox[2] <= rect[2], node_id
        assert rect[1] <= bbox[1] and bbox[3] <= rect[3], node_id


def test_public_api_accepts_pages_beyond_twenty_and_saves_full_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    pdf = authored_pdf(tmp_path / "operations.pdf", page_count=22, label="Operations")
    result = ingest_pdf(pdf=pdf, pages="1,21-22", output_dir=tmp_path / "output")

    assert result.source_sha256 == sha256(pdf.read_bytes()).hexdigest()
    assert result.source_page_count == 22
    assert result.selected_physical_pages == (1, 21, 22)
    assert result.stage == "source"
    assert result.live_call_count == 0
    assert result.activated is False
    assert result.source_cached is False
    assert result.source_store == str(tmp_path / "output" / result.source_sha256 / "source")
    sources = LocalDocumentStore(Path(result.source_store))
    source = sources.load(result.source_manifest_id)
    assert len(source.manifest.pages) == 22
    assert source.manifest.filename == "operations.pdf"
    outputs = ProcessingStore(Path(result.processing_store))
    manifest = outputs.load(result.processing_id)
    assert manifest.retrieval is None
    assert tuple(page.page_index for page in manifest.pages) == (0, 20, 21)
    assert all(page.partition.state is StageState.DEFERRED for page in manifest.pages)
    assert all(not page.objects and page.raw_partition is None for page in manifest.pages)
    canonical_ref = manifest.pages[-1].canonical.artifact
    assert canonical_ref is not None
    canonical = TypeAdapter(CanonicalPage).validate_json(outputs.assets.get(canonical_ref))
    assert canonical.page_index == 21
    assert canonical.text[0].text == "Operations page 22"
    assert not (sources.root / "current-manifest").exists()
    assert not (outputs.root / "current-processing").exists()
    review = Path(result.review_path).read_text()
    assert "operations.pdf" in review
    assert "前 20 页" not in review


def test_script_and_api_isolate_documents_and_resume_without_source_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = authored_pdf(tmp_path / "first.pdf", page_count=2, label="First")
    second = authored_pdf(tmp_path / "second.pdf", page_count=1, label="Second")
    environment = {
        **os.environ,
        "APP_ROOT_DIR": str(tmp_path),
        "APP_DATA_DIR": str(tmp_path / "data"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "enterprise_pdf_rag" / "ingest.py"),
            "--pdf",
            str(first),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = IngestionSummary.model_validate_json(completed.stdout)
    assert result.selected_physical_pages == (1, 2)
    assert Path(result.source_store).is_relative_to(tmp_path / "data" / "ingestion")
    other = ingest_pdf(pdf=second, output_dir=tmp_path / "data" / "ingestion")
    assert other.source_sha256 != result.source_sha256
    assert other.source_store != result.source_store
    assert other.processing_store != result.processing_store

    def no_reextract(_self: PdfspineDocumentAdapter, _pdf: bytes) -> None:
        pytest.fail("A valid source cache must not re-extract its PDF")

    monkeypatch.setattr(PdfspineDocumentAdapter, "extract_document", no_reextract)
    resumed = ingest_pdf(pdf=first, output_dir=tmp_path / "data" / "ingestion")
    assert resumed.source_cached is True
    assert resumed.processing_id == result.processing_id
    assert resumed.source_manifest_id == result.source_manifest_id
    assert resumed.live_call_count == 0
    source_store = LocalDocumentStore(Path(resumed.source_store))
    source = source_store.load(resumed.source_manifest_id)
    source_store.asset_path(source.manifest.pages[0].svg).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest mismatch"):
        ingest_pdf(pdf=first, output_dir=tmp_path / "data" / "ingestion")


@pytest.mark.parametrize("pages", ["0", "3", "2-1", "1,1", "1-3", "all,1", "", "one"])
def test_invalid_selected_pages_do_not_write_outputs(tmp_path: Path, pages: str) -> None:
    pdf = authored_pdf(tmp_path / "two.pdf", page_count=2, label="Two")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=r"[Pp]age"):
        ingest_pdf(pdf=pdf, pages=pages, output_dir=output)
    assert not output.exists()


def test_invalid_input_and_implicit_live_budget_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PDF file"):
        ingest_pdf(pdf=tmp_path / "missing.pdf", output_dir=tmp_path / "output")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="PDF"):
        ingest_pdf(pdf=bad, output_dir=tmp_path / "output")
    pdf = authored_pdf(tmp_path / "one.pdf", page_count=1, label="One")
    with pytest.raises(ValueError, match=r"source.*budget"):
        ingest_pdf(pdf=pdf, max_live_calls=1, output_dir=tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_unsupported_source_coordinates_fail_explicitly_without_a_draft(
    tmp_path: Path,
) -> None:
    pdf = authored_pdf(tmp_path / "rotated.pdf", page_count=1, label="Rotated")
    with pdfspine.open(stream=pdf.read_bytes()) as document:
        document.load_page(0).set_rotation(90)
        pdf.write_bytes(document.tobytes())
    with pytest.raises(ValueError, match="Page index 0 extraction failed: Rotated pages"):
        ingest_pdf(pdf=pdf, output_dir=tmp_path / "output")
    assert not tuple((tmp_path / "output").rglob("current-*"))
    assert not tuple((tmp_path / "output").rglob("processing"))


def test_explicit_stages_share_budget_preserve_partial_branches_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = authored_pdf(tmp_path / "metric.pdf", page_count=1, label="Metric", embedded_font=True)
    for key, value in {
        "OPENAI_API_KEY": "unit-secret",
        "OPENAI_BASE_URL": "https://provider.invalid",
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    calls: list[bytes] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == "https://provider.invalid/v1/chat/completions"
        calls.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        content: dict[str, object]
        if "Source text observations:" in prompt:
            content = {
                "regions": [
                    {
                        "region_id": "figure",
                        "kind": "Chart",
                        "bbox": [0.0, 0.0, 240.0, 160.0],
                        "source_span_ids": ["s0000"],
                        "context_span_ids": [],
                        "list_items": [],
                        "list_ordered": None,
                        "parent_id": None,
                        "interpretation": "Chart hypothesis requiring review",
                    }
                ],
                "unassigned_span_ids": [],
                "diagnostics": [],
            }
        else:
            view = json.loads(prompt.rsplit("\n", 1)[1])
            evidence = {
                "element_ids": [view["observations"][0]["id"]],
                "confidence": "high",
            }
            content = (
                {
                    "schema_version": "chart-observations-v1",
                    "svg_digest": view["svg_digest"],
                    "grammar": "bar",
                    "title": {"text": "Metric page 1", "evidence": evidence},
                    "period": None,
                    "axes": [],
                    "points": [],
                    "marks": [],
                    "diagnostics": [],
                }
                if "chart-observations-v1" in prompt
                else {
                    "schema_version": "figure-description-v1",
                    "svg_digest": view["svg_digest"],
                    "claims": [
                        {
                            "text": "Metric page 1",
                            "evidence": evidence,
                            "series": None,
                            "category": None,
                            "unit": None,
                            "value": None,
                            "period": None,
                        }
                    ],
                    "diagnostics": [],
                }
            )
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(content)},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    output = tmp_path / "output"
    empty = ingest_pdf(pdf=pdf, output_dir=output, stage="layout")
    assert empty.live_call_count == 0 and empty.failed_stage_count == 1
    assert not calls
    partial = ingest_pdf(pdf=pdf, output_dir=output, stage="semantics", max_live_calls=2)
    assert partial.live_call_count == len(calls) == 2
    assert partial.failed_stage_count >= 1
    outputs = ProcessingStore(Path(partial.processing_store))
    manifest = outputs.load(partial.processing_id)
    assert manifest.pages[0].raw_partition is None
    stages = {value.stage: value for value in manifest.pages[0].objects[0].stages}
    assert stages["ir"].state is StageState.SUCCEEDED
    assert stages["description"].state is not StageState.SUCCEEDED
    assert stages["ir_raw"].artifact is not None
    complete = ingest_pdf(pdf=pdf, output_dir=output, stage="semantics", max_live_calls=1)
    assert complete.live_call_count == 1
    assert len(calls) == 3
    assert complete.failed_stage_count == 0
    assert complete.indexed is False and complete.activated is False
    manifest = outputs.load(complete.processing_id)
    record = manifest.pages[0].objects[0]
    assert record.qualified_claim_count == 0 and manifest.retrieval is None
    stages = {value.stage: value for value in record.stages}
    assert stages["ir"].state is stages["description"].state is StageState.SUCCEEDED
    assert stages["qualification"].state is StageState.UNAVAILABLE
    replay = ingest_pdf(pdf=pdf, output_dir=output, stage="semantics")
    assert replay.processing_id == complete.processing_id
    assert replay.live_call_count == 0 and len(calls) == 3
    assert not (outputs.root / "current-processing").exists()


def test_script_reports_invalid_range_without_traceback_or_output(
    tmp_path: Path,
) -> None:
    pdf = authored_pdf(tmp_path / "one.pdf", page_count=1, label="One")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "enterprise_pdf_rag" / "ingest.py"),
            "--pdf",
            str(pdf),
            "--pages",
            "2",
            "--output-dir",
            str(tmp_path / "out"),
        ],
        cwd=tmp_path,
        env={**os.environ, "APP_ROOT_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "Pages must exist" in completed.stderr and "Traceback" not in completed.stderr
    assert not (tmp_path / "out").exists()
