"""run-folder makes a partial ingest visible and reports page progress (ADR 0022)."""

import shutil
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.pdf_ingestion import MAX_INGEST_LIVE_CALLS
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _OFFLINE,
    _PAGES,
    _PER_PDF,
    _model_env,
    _pdf,
)

_TEXT_KEYS = {"answer", "value", "text", "content", "prompt", "completion", "chunk", "body"}


def _run(
    tmp_path: Path, folder: Path, budget: int, **options: object
) -> tuple[FolderPipelineResult, list[tuple[str, dict[str, object]]]]:
    events: list[tuple[str, dict[str, object]]] = []
    result = run_folder_pipeline(
        folder,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=budget,
        build_tree=False,
        embedder=_OFFLINE,
        progress=lambda event, payload: events.append((event, payload)),
        **options,  # type: ignore[arg-type]
    )
    return result, events


def test_the_per_pdf_ceiling_is_raised_for_reports_of_several_hundred_pages() -> None:
    assert MAX_INGEST_LIVE_CALLS == 10_000


def test_a_budget_cut_document_is_published_but_says_how_much_was_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "meridian.pdf", _MERIDIAN)

    # Three layout calls and one metadata call: two pages wait for the next round.
    partial, events = _run(tmp_path, folder, _PAGES + 1)

    (document,) = partial.documents
    assert document.status == "published" and document.error is None
    assert document.ingestion is not None
    assert (
        document.ingestion.pages_complete,
        document.ingestion.pages_budget_deferred,
        document.ingestion.pages_claim_blocked,
    ) == (1, _PAGES - 1, 0)
    (done,) = [payload for event, payload in events if event == "document_done"]
    assert done["pages"] == f"1/{_PAGES}" and done["pages_budget_deferred"] == _PAGES - 1

    complete, _ = _run(tmp_path, folder, _PER_PDF)
    assert complete.documents[0].ingestion is not None
    assert complete.documents[0].ingestion.pages_complete == _PAGES
    assert complete.live_calls.ingest == _PAGES - 1


def test_page_progress_reports_counts_only_and_marks_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "meridian.pdf", _MERIDIAN)

    _, events = _run(tmp_path, folder, _PER_PDF)
    progress = [payload for event, payload in events if event == "document_progress"]

    pages = [item for item in progress if "pages_done" in item]
    # The first and the last page of each of the two page stages (three pages, throttled).
    assert [(item["stage"], item["pages_done"], item["pages_total"]) for item in pages] == [
        ("layout", 1, _PAGES),
        ("layout", _PAGES, _PAGES),
        ("metadata", 1, _PAGES),
        ("metadata", _PAGES, _PAGES),
    ]
    assert pages[-1]["live_calls"] == _PER_PDF and pages[-1]["budget"] == _PER_PDF
    assert [item["stage"] for item in progress if "pages_done" not in item] == [
        "requalify",
        "qualify",
        "index",
        "publish",
    ]
    assert all(not _TEXT_KEYS & set(item) for item in progress)

    # A finished page replays from the stage cache without asking the model at all; with the
    # stage cache gone it is the model cache that answers, and that is what cache_hits counts.
    (stage_cache,) = (tmp_path / "ingestion").glob("*/processing/stage-cache")
    shutil.rmtree(stage_cache)
    _, replay = _run(tmp_path, folder, _PER_PDF)
    last = [payload for event, payload in replay if event == "document_progress"][1]
    assert (last["stage"], last["live_calls"], last["cache_hits"]) == ("layout", 0, _PAGES)
