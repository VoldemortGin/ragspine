"""Lite ingest (ADR 0025): every kind of fact stays answerable, cited to its page."""

from pathlib import Path

import pytest

from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    lite_env,
    mixed_folder,
    run_mode,
    write_questions,
)


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
