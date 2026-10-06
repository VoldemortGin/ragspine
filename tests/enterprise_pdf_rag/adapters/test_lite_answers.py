"""Lite ingest (ADR 0025): every kind of fact stays answerable, cited to its page."""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    claim_script,
    lite_env,
    mixed_folder,
    run_mode,
    write_questions,
)
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client


def test_lite_answers_charts_tables_formulas_and_period_questions_with_their_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)

    result = run_mode(tmp_path, "lite", questions=write_questions(tmp_path))

    assert result.eval is not None
    cases = {case.case_id: case for case in result.eval.cases}
    expected_pages = {"chart": 4, "ruled": 2, "unruled": 3, "period": 1, "formula": 6}
    for case_id, page in expected_pages.items():
        case = cases[case_id]
        assert (case.verdict, case.failures, case.cited_pages) == ("answered", (), (page,)), case_id
        assert case.claim_count == 1
    # Lite indexes the unruled table as its verbatim printed rows (ADR 0027), so its figure is
    # answered from a row quote; full still leaves the table out (see test_lite_ingest).
    (document,) = result.documents
    assert document.ingestion is not None
    assert (document.ingestion.table_row_transcriptions, document.ingestion.table_row_lines) == (
        1,
        3,
    )


def test_lite_with_the_deterministic_layout_answers_all_but_the_formula_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notebook default: lite with ``layout_policy="deterministic-text-pages"`` (ADR 0028).

    Known limit, pinned rather than hidden: the deterministic layout proposes no Formula
    object, so a formula page with no graphics is partitioned as text. Its text stays quotable,
    but a claim citing it as a formula has nothing to cite and the question abstains;
    ``LAYOUT_POLICY = "model"`` brings the Formula object back.
    """
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    llm, _ = scripted_client(tmp_path / "answers", claim_script, max_live_calls=20)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        questions=write_questions(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        ingest_mode="lite",
        layout_policy="deterministic-text-pages",
    )
    (document,) = result.documents
    assert document.status == "published" and document.ingestion is not None
    # Text, ruled, unruled and formula pages need no layout call; chart, image, diagram do.
    assert document.ingestion.pages_partitioned_deterministically == 4
    assert document.ingestion.partition_fallback_reasons == {"residual_graphics": 3}
    assert tasks["page-layout"] == document.ingestion.pages_partition_model_fallback
    assert result.eval is not None
    verdicts = {case.case_id: case.verdict for case in result.eval.cases}
    assert verdicts == {
        "chart": "answered",
        "unruled": "answered",
        "ruled": "answered",
        "period": "answered",
        "formula": "abstained",
    }
