"""Lite ingest (ADR 0025): no tree by default, no review pages unless asked for."""

from pathlib import Path
from typing import Never

import pytest

from enterprise_pdf_rag.adapters.pdf_ingestion import export_document_review
from ragspine.extraction.evidence.page.models import StageState
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    lite_env,
    mixed_folder,
    processing_store_of,
    run_mode,
)

# ---- 3. tree, review exports ---------------------------------------------------------------------


def test_lite_builds_no_tree_by_default_and_full_still_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.folder_pipeline.annotate_document_tree", _no_tree
    )

    lite = run_mode(tmp_path, "lite", build_tree=None)

    assert lite.documents[0].tree is None and lite.live_calls.tree == 0
    with pytest.raises(AssertionError, match="tree"):
        run_mode(tmp_path, "full", build_tree=None, root="full")


def test_an_explicit_tree_in_lite_is_built_from_the_deterministic_page_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)

    result = run_mode(tmp_path, "lite", build_tree=True)

    (document,) = result.documents
    assert document.status == "published" and document.error is None
    assert document.tree is not None and document.tree.state is StageState.SUCCEEDED
    assert document.tree.page_count == 7
    assert tasks["tree-summary"] == document.tree.live_call_count == result.live_calls.tree


def _no_tree(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("tree must not be built")


def test_lite_writes_no_review_pages_and_far_fewer_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)

    full = run_mode(tmp_path, "full", root="full")
    lite = run_mode(tmp_path, "lite", root="lite")

    (full_document,), (lite_document,) = full.documents, lite.documents
    assert full_document.ingestion is not None and lite_document.ingestion is not None
    assert full_document.ingestion.review_path is not None
    assert full_document.index is not None and full_document.index.review_path is not None
    assert lite_document.ingestion.review_path is None
    assert lite_document.index is not None and lite_document.index.review_path is None
    lite_store = processing_store_of(tmp_path, "lite")
    assert not (lite_store / "runs").exists() and not (lite_store / "review.html").exists()
    assert not (lite_store.parent / "source" / "source.pdf").exists()
    full_files = sum(1 for path in (tmp_path / "full").rglob("*") if path.is_file())
    lite_files = sum(1 for path in (tmp_path / "lite").rglob("*") if path.is_file())
    assert lite_files < full_files * 0.6, (lite_files, full_files)


def test_a_lite_document_review_is_written_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    (document,) = run_mode(tmp_path, "lite").documents
    assert document.ingestion is not None and document.publication is not None

    review = export_document_review(Path(document.ingestion.processing_store).parent)

    assert review.name == "review.html" and "mixed.pdf" in review.read_text()
    assert (Path(document.ingestion.source_store) / "review.html").is_file()
