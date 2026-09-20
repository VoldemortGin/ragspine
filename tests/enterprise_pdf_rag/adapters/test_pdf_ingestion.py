"""Generic ingestion preserves source identity without model or publication effects."""

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import cast

import pdfspine
import pytest
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.core.settings import ROOT_DIR
from enterprise_pdf_rag.processing.models import CanonicalPage, StageState
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage


def authored_pdf(path: Path, *, page_count: int, label: str, embedded_font: bool = False) -> Path:
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
        path.write_bytes(document.tobytes())
    return path


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

    monkeypatch.setattr("enterprise_pdf_rag.adapters.json_completion._send_once", sender)
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
