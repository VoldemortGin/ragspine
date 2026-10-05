"""Lite ingest (ADR 0025): the calls it stops, what it derives instead, and full left untouched."""

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Never

import pytest

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.ingest_mode import IngestPlan, check_ingest_mode, ingest_plan
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    FULL_STORE_DIGEST,
    FULL_STORE_FILES,
    FULL_TASKS,
    LITE_TASKS,
    SKIPPED_CALLS,
    claim_script,
    lite_env,
    mixed_folder,
    run_mode,
    store_digest,
)
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client


def test_full_mode_writes_byte_for_byte_what_the_release_before_lite_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    result = run_folder_pipeline(
        mixed_folder(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        build_tree=False,
        embedder=OfflineDescriptionEmbedder(),
    )

    (document,) = result.documents
    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert dict(tasks) == FULL_TASKS
    digest, count, requests = store_digest(tmp_path / "ingestion")
    nonvolatile = count - sum(1 for _ in (tmp_path / "ingestion").rglob("contexts/*.json"))
    assert (digest, nonvolatile, requests) == (
        FULL_STORE_DIGEST,
        FULL_STORE_FILES,
        FULL_REQUESTS_DIGEST,
    )
    assert isinstance(result, FolderPipelineResult)


# ---- 0. the mode switch --------------------------------------------------------------------


def test_an_explicit_full_mode_is_the_default_and_an_unknown_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)

    result = run_mode(tmp_path, "full")

    (document,) = result.documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (result.ingest_mode, document.ingest_mode, document.published_ingest_mode) == (
        "full",
        "full",
        "full",
    )
    assert document.ingestion is not None and document.ingestion.skipped_calls == {}
    # The runtime type check (beartype) or ``check_ingest_mode`` refuses it before any work.
    with pytest.raises(Exception, match="'fast'"):
        run_mode(tmp_path, "fast")


def test_ingest_plans_name_every_switch_and_lite_keeps_the_model_layout() -> None:
    full, lite = ingest_plan("full"), ingest_plan("lite")
    assert (full.layout, lite.layout) == ("model", "model")
    assert full == IngestPlan("full")
    assert (
        lite.image_semantics,
        lite.formula_semantics,
        lite.chart_description,
        lite.page_metadata,
        lite.review_exports,
        lite.build_tree,
        lite.unverified_tables_as_rows,
    ) == (False, False, "from-ir", "deterministic", False, False, True)
    # Full keeps its bytes: no row transcription; the deterministic layout is never a preset.
    assert full.unverified_tables_as_rows is False
    assert ingest_plan("lite", layout_policy="deterministic-text-pages").layout == (
        "deterministic-text-pages"
    )
    assert ingest_plan("full", unverified_tables_as_rows=True).unverified_tables_as_rows
    with pytest.raises(Exception, match="'fast'"):
        ingest_plan("lite", layout_policy="fast")  # type: ignore[arg-type]
    with pytest.raises(Exception, match="'fast'"):
        ingest_plan("fast")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingest_mode must be one of"):
        check_ingest_mode("fast")


# ---- 1. the calls lite stops -----------------------------------------------------------------


def test_lite_sends_only_layout_chart_ir_and_diagram_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)

    result = run_mode(tmp_path, "lite")

    (document,) = result.documents
    assert document.status == "published" and document.error is None
    assert dict(tasks) == LITE_TASKS
    assert document.live_calls == sum(LITE_TASKS.values()) == result.live_calls.ingest
    assert document.ingestion is not None
    assert document.ingestion.skipped_calls == SKIPPED_CALLS
    assert document.ingestion.pages_complete == 7
    assert (result.ingest_mode, document.ingest_mode, document.published_ingest_mode) == (
        "lite",
        "lite",
        "lite",
    )


# ---- mode coexistence ----------------------------------------------------------------------------


def test_full_after_lite_sends_only_the_calls_lite_skipped_and_republishes_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "lite")
    tasks.clear()

    full = run_mode(tmp_path, "full")

    assert dict(tasks) == {
        name: count for name, count in FULL_TASKS.items() if name not in LITE_TASKS
    }
    (document,) = full.documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert document.published_ingest_mode == "full"


def test_lite_after_full_makes_no_live_call_and_republishes_lite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "full")
    tasks.clear()

    lite = run_mode(tmp_path, "lite")

    assert dict(tasks) == {} and lite.live_calls.total == 0
    (document,) = lite.documents
    assert document.status == "published" and document.published_ingest_mode == "lite"
    assert document.ingestion is not None and document.ingestion.skipped_calls == SKIPPED_CALLS


def test_a_failed_lite_rerun_reports_the_full_release_still_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "full")
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.folder_pipeline.qualify_draft", _broken_qualify
    )

    lite = run_mode(tmp_path, "lite")

    (document,) = lite.documents
    assert (document.status, document.failed_stage) == ("failed", "qualify")
    assert (document.ingest_mode, document.published_ingest_mode) == ("lite", "full")


def _broken_qualify(**_kwargs: object) -> Never:
    raise ValueError("qualification refused")


# ---- 5. visibility ---------------------------------------------------------------------------------


def test_the_report_and_progress_events_name_the_mode_and_the_skipped_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    llm, _ = scripted_client(tmp_path / "answers", claim_script, max_live_calls=20)

    result = run_folder_pipeline(
        tmp_path / "pdfs",
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf="auto",
        ingest_mode="lite",
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        report_dir=tmp_path / "report",
        progress=lambda event, payload: events.append((event, payload)),
    )

    assert result.ingest_mode == "lite"
    markdown = (tmp_path / "report" / "report.md").read_text()
    assert "- ingest mode: **lite**" in markdown
    assert "| lite | lite |" in markdown
    assert "chart_description 1, formula 4, image 2, page_metadata 7" in markdown
    starts = [payload for event, payload in events if event in ("discovered", "document_start")]
    assert starts and all(payload["ingest_mode"] == "lite" for payload in starts)


def test_the_cli_passes_the_ingest_mode_and_lets_the_mode_choose_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def fake(*_args: object, **kwargs: object) -> FolderPipelineResult:
        seen.append(kwargs)
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "run_folder_pipeline", fake)
    base = ["run-folder", "--max-live-calls-per-pdf", "auto"]
    for arguments in (
        base,
        [*base, "--ingest-mode", "lite"],
        [*base, "--ingest-mode", "lite", "--tree"],
        [*base, "--no-tree"],
    ):
        with redirect_stdout(io.StringIO()):
            assert cli.main(arguments) == 1
    assert [(item["ingest_mode"], item["build_tree"]) for item in seen] == [
        ("full", None),
        ("lite", None),
        ("lite", True),
        ("full", False),
    ]
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit):
        cli.main([*base, "--ingest-mode", "fast"])
