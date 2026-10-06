"""run-folder makes a partial ingest visible and reports page progress (ADR 0022)."""

import shutil
from pathlib import Path

import pytest
from beartype.roar import BeartypeCallHintViolation

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters.folder_pipeline import (
    AUTO_CALLS_BASE,
    AUTO_CALLS_PER_PAGE,
    FolderPipelineResult,
    auto_live_call_budget,
    run_folder_pipeline,
)
from enterprise_pdf_rag.adapters.pdf_ingestion import MAX_INGEST_LIVE_CALLS
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _OFFLINE,
    _PAGES,
    _PER_PDF,
    _model_env,
    _pdf,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf

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
    (stage_cache,) = (tmp_path / "ingestion").glob("*/processing/stage-cache-sharded")
    assert not stage_cache.with_name("stage-cache").exists()
    shutil.rmtree(stage_cache)
    _, replay = _run(tmp_path, folder, _PER_PDF)
    last = [payload for event, payload in replay if event == "document_progress"][1]
    assert (last["stage"], last["live_calls"], last["cache_hits"]) == ("layout", 0, _PAGES)


# ---- "auto": the per-PDF ceiling follows the page count ----------------------------------


def test_auto_is_pages_times_four_plus_fifty_capped_at_the_ceiling() -> None:
    assert (AUTO_CALLS_PER_PAGE, AUTO_CALLS_BASE) == (4, 50)
    assert [auto_live_call_budget(pages) for pages in (3, 300, 2487, 2488, 5000)] == [
        62,
        1250,
        9998,
        MAX_INGEST_LIVE_CALLS,
        MAX_INGEST_LIVE_CALLS,
    ]


def test_auto_grants_each_pdf_its_own_computed_budget_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "a.pdf", _MERIDIAN)
    authored_pdf(folder / "b.pdf", page_count=5, label="Orion FY2024", embedded_font=True)

    result, events = _run(tmp_path, folder, "auto")  # type: ignore[arg-type]

    assert [item.live_call_budget for item in result.documents] == [62, 70]
    assert [payload["budget"] for event, payload in events if event == "document_start"] == [
        62,
        70,
    ]
    assert all(item.status == "published" for item in result.documents)
    pages = [payload for event, payload in events if "pages_done" in payload]
    assert {payload["budget"] for payload in pages} == {62, 70}


def test_auto_counts_only_the_selected_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "a.pdf", _MERIDIAN)
    result, _ = _run(tmp_path, folder, "auto", pages="1-2")  # type: ignore[arg-type]
    assert result.documents[0].live_call_budget == 2 * 4 + 50


def test_auto_is_still_bounded_by_the_shared_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "a.pdf", _MERIDIAN)
    _pdf(folder / "b.pdf", "Orion FY2024 Thailand")

    result, _ = _run(tmp_path, folder, "auto", max_live_calls_total=_PER_PDF)  # type: ignore[arg-type]

    first, second = result.documents
    assert first.live_call_budget == _PER_PDF and first.status == "published"
    assert second.live_call_budget == 0 and second.status == "budget_starved"
    assert result.budget_exhausted


@pytest.mark.parametrize("bad", ["Auto", "pages", "", -1, MAX_INGEST_LIVE_CALLS + 1])
def test_an_invalid_per_pdf_budget_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    calls = _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "a.pdf", _MERIDIAN)
    # A string other than "auto" is already refused by the runtime type check (beartype).
    with pytest.raises((ValueError, BeartypeCallHintViolation), match="max_live_calls_per_pdf"):
        _run(tmp_path, folder, bad)  # type: ignore[arg-type]
    assert calls == [] and not (tmp_path / "ingestion").exists()


def test_the_cli_takes_auto_or_an_integer() -> None:
    parse = cli._parser().parse_args
    for value, expected in (("auto", "auto"), ("60", 60)):
        assert parse(["run-folder", "--max-live-calls-per-pdf", value]).max_live_calls_per_pdf == (
            expected
        )
    with pytest.raises(SystemExit):
        parse(["run-folder", "--max-live-calls-per-pdf", "Auto"])
