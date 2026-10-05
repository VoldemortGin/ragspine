"""run-folder: which PDFs the questions name, checked before any work (ADR 0022).

One resolution (``adapters/question_docs.py``) decides both what ``only_question_docs``
ingests and where each answer is routed, so a question whose PDF was ingested is exactly a
question that routes to it.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import (
    FolderPipelineResult,
    check_question_docs,
    run_folder_pipeline,
)
from enterprise_pdf_rag.adapters.question_docs import QuestionDocsError, describe
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _OFFLINE,
    _ORION,
    _PER_PDF,
    _answer_llm,
    _gold,
    _model_env,
    _pdf,
)

_ATLAS = "Atlas FY2025 Japan"


def _three(tmp_path: Path) -> Path:
    folder = tmp_path / "pdfs"
    _pdf(folder / "Meridian Interim 2024.pdf", _MERIDIAN)
    _pdf(folder / "orion_fy2024.pdf", _ORION)
    _pdf(folder / "atlas.pdf", _ATLAS)
    return folder


def _questions(tmp_path: Path, *rows: dict[str, object]) -> Path:
    path = tmp_path / "questions.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _q(identifier: str, doc: str | None, page: int = 2) -> dict[str, object]:
    row: dict[str, object] = {"id": identifier, "question": f"What does page {page} say?"}
    if doc is not None:
        row["doc"] = doc
    return row


def _run(
    tmp_path: Path, folder: Path, questions: Path, **options: object
) -> tuple[FolderPipelineResult, list[tuple[str, dict[str, object]]]]:
    events: list[tuple[str, dict[str, object]]] = []
    llm, _ = _answer_llm(tmp_path)
    result = run_folder_pipeline(
        folder,
        questions=questions,
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=_OFFLINE,
        answer_llm=llm,
        progress=lambda event, payload: events.append((event, payload)),
        **options,  # type: ignore[arg-type]
    )
    return result, events


def _statuses(result: FolderPipelineResult) -> dict[str, str]:
    return {Path(item.pdf_path).name: item.status for item in result.documents}


def test_only_question_docs_ingests_the_named_pdfs_and_never_reads_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _model_env(monkeypatch)
    folder = _three(tmp_path)
    # A loose spelling (case, separators, no extension) and a stem.
    questions = _questions(tmp_path, _q("q1", "MERIDIAN-interim_2024"), _q("q2", "orion_fy2024"))
    if os.name == "posix":
        (folder / "atlas.pdf").chmod(0)  # any read of the skipped PDF would raise

    try:
        result, events = _run(tmp_path, folder, questions, only_question_docs=True)
    finally:
        (folder / "atlas.pdf").chmod(0o644)

    assert _statuses(result) == {
        "Meridian Interim 2024.pdf": "published",
        "orion_fy2024.pdf": "published",
        "atlas.pdf": "skipped_not_referenced",
    }
    skipped = next(item for item in result.documents if item.status == "skipped_not_referenced")
    assert skipped.sha256 is None and skipped.live_calls == 0
    assert len(calls) == 2 * 3 and result.ok
    assert result.eval is not None
    assert [case.verdict for case in result.eval.cases] == ["answered", "answered"]
    assert result.question_docs is not None
    assert result.question_docs.rule_counts == {"normalized": 1, "stem": 1}
    (resolved,) = [payload for event, payload in events if event == "question_docs_resolved"]
    assert resolved["matched"] == 2 and resolved["unmatched"] == []
    assert [event for event, _ in events].index("question_docs_resolved") < [
        event for event, _ in events
    ].index("discovered")


def test_an_unmatched_doc_stops_before_any_write_or_model_call_with_the_closest_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "Meridian Interim 2023"), _q("q2", "orion_fy2024"))

    with pytest.raises(QuestionDocsError) as raised:
        _run(tmp_path, folder, questions, only_question_docs=True)

    message = str(raised.value)
    assert "'Meridian Interim 2023': 文件夹里找不到" in message
    assert "Meridian Interim 2024.pdf (0.9" in message and "q1" in message
    for way_out in ("DOC_ALIASES", "ONLY_QUESTION_DOCS", "ON_UNMATCHED_DOCS"):
        assert way_out in message
    assert calls == [] and not (tmp_path / "ingestion").exists()


def test_an_alias_fixes_a_near_miss_for_both_ingest_and_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "Meridian Interim 2023"))

    result, _ = _run(
        tmp_path,
        folder,
        questions,
        only_question_docs=True,
        doc_aliases={"meridian interim 2023": "Meridian Interim 2024.pdf"},
    )

    assert _statuses(result)["Meridian Interim 2024.pdf"] == "published"
    assert result.eval is not None and result.eval.cases[0].verdict == "answered"
    meridian = next(item for item in result.documents if item.status == "published")
    assert result.eval.cases[0].document_id == meridian.sha256
    assert result.question_docs is not None and result.question_docs.rule_counts == {"alias": 1}


def test_an_alias_naming_no_pdf_is_refused_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "x"))
    with pytest.raises(QuestionDocsError, match=r"'x': 'missing\.pdf'"):
        _run(tmp_path, folder, questions, doc_aliases={"x": "missing.pdf"})
    assert calls == [] and not (tmp_path / "ingestion").exists()


def test_skip_ingests_what_matched_and_marks_the_unmatched_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(
        tmp_path, _q("q1", "Meridian Interim 2023"), _q("q2", "orion_fy2024"), _q("q3", None)
    )

    result, _ = _run(tmp_path, folder, questions, only_question_docs=True, on_unmatched_docs="skip")

    assert _statuses(result) == {
        "Meridian Interim 2024.pdf": "skipped_not_referenced",
        "orion_fy2024.pdf": "published",
        "atlas.pdf": "skipped_not_referenced",
    }
    assert result.eval is not None
    cases = {case.case_id: case for case in result.eval.cases}
    assert cases["q1"].verdict == "routing_failed"
    assert "'Meridian Interim 2023' names no PDF of the folder" in cases["q1"].failures[0]
    assert "Meridian Interim 2024.pdf" in cases["q1"].failures[0]
    assert cases["q2"].verdict == "answered"
    # No doc and a single published document: the existing routing still answers it there.
    assert cases["q3"].verdict == "answered"
    assert result.question_docs is not None
    assert result.question_docs.questions_without_doc == ("q3",)


def test_a_question_without_doc_stops_only_question_docs_in_error_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "atlas"), _q("q2", None))
    with pytest.raises(QuestionDocsError, match=r"1 道题没有写 doc.*q2"):
        _run(tmp_path, folder, questions, only_question_docs=True)
    assert calls == [] and not (tmp_path / "ingestion").exists()


def test_two_pdfs_under_one_name_are_ambiguous_and_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = tmp_path / "pdfs"
    _pdf(folder / "2024/report.pdf", _MERIDIAN)
    _pdf(folder / "2025/report.pdf", _ORION)
    questions = _questions(tmp_path, _q("q1", "report"))
    with pytest.raises(QuestionDocsError, match="歧义"):
        _run(tmp_path, folder, questions, only_question_docs=True)


def test_without_only_question_docs_misses_are_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "Meridian Interim 2023"), _q("q2", "atlas"))

    result, events = _run(tmp_path, folder, questions)

    assert set(_statuses(result).values()) == {"published"}
    assert result.question_docs is not None
    assert [item.reference for item in result.question_docs.unresolved] == ["Meridian Interim 2023"]
    (resolved,) = [payload for event, payload in events if event == "question_docs_resolved"]
    assert resolved["unmatched"] == ["Meridian Interim 2023"]
    assert result.eval is not None
    assert [case.verdict for case in result.eval.cases] == ["routing_failed", "answered"]


def test_first_matched_skips_questions_whose_pdf_is_missing_and_keeps_set_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(
        tmp_path,
        _q("q1", "absent-one.pdf"),
        _q("q2", "Absent Two"),
        _q("q3", None),
        _q("q4", "atlas"),
        _q("q5", "orion_fy2024.pdf"),
        _q("q6", "Meridian Interim 2024"),
    )

    result, _ = _run(
        tmp_path,
        folder,
        questions,
        max_questions=2,
        question_selection="first_matched",
        only_question_docs=True,
    )

    assert result.eval is not None
    assert [case.case_id for case in result.eval.cases] == ["q4", "q5"]
    assert _statuses(result) == {
        "Meridian Interim 2024.pdf": "skipped_not_referenced",
        "orion_fy2024.pdf": "published",
        "atlas.pdf": "published",
    }
    assert result.question_docs is not None
    selection = result.question_docs.selection
    assert selection is not None and not selection.short
    assert [(item.question_id, item.pdf, item.rules) for item in selection.selected] == [
        ("q4", "atlas.pdf", ("stem",)),
        ("q5", "orion_fy2024.pdf", ("exact",)),
    ]
    assert [item.question_id for item in selection.skipped] == ["q1", "q2", "q3"]
    assert "没有写 doc" in selection.skipped[2].reason


def test_first_matched_runs_what_it_found_and_says_the_set_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "absent"), _q("q2", "atlas"))

    result, _ = _run(
        tmp_path,
        folder,
        questions,
        max_questions=5,
        question_selection="first_matched",
        report_dir=tmp_path / "report",
    )

    assert result.eval is not None and [case.case_id for case in result.eval.cases] == ["q2"]
    assert result.question_docs is not None and result.question_docs.selection is not None
    assert result.question_docs.selection.short
    assert "题集中只有 1 道题的文档在文件夹里" in describe(result.question_docs)
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    selection = report["question_docs"]["selection"]
    assert selection["mode"] == "first_matched"
    assert [item["question_id"] for item in selection["skipped"]] == ["q1"]
    assert "What does page" not in json.dumps(report["question_docs"])


def test_first_matched_with_nothing_to_run_stops_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "absent"), _q("q2", None))
    with pytest.raises(QuestionDocsError, match="没有可跑的题"):
        _run(tmp_path, folder, questions, max_questions=3, question_selection="first_matched")
    assert calls == [] and not (tmp_path / "ingestion").exists()


def test_first_matched_takes_every_question_when_there_is_no_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "absent"), _q("q2", "atlas"))
    result, _ = _run(tmp_path, folder, questions, question_selection="first_matched")
    assert result.eval is not None and [case.case_id for case in result.eval.cases] == ["q1", "q2"]


def test_an_alias_makes_a_skipped_question_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "Atlas 2025"), _q("q2", "orion_fy2024"))
    result, _ = _run(
        tmp_path,
        folder,
        questions,
        max_questions=1,
        question_selection="first_matched",
        doc_aliases={"Atlas 2025": "atlas"},
    )
    assert result.eval is not None and [case.case_id for case in result.eval.cases] == ["q1"]


def test_a_gold_set_selects_and_ingests_by_document_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _three(tmp_path)
    first, _ = _run(tmp_path, folder, _questions(tmp_path, _q("q", "atlas")))
    meridian = next(
        item.sha256 for item in first.documents if item.pdf_path.endswith("Interim 2024.pdf")
    )
    assert meridian is not None
    payload = _gold(meridian)
    cases = payload["cases"]
    assert isinstance(cases, list)
    cases.insert(0, {**cases[1], "case_id": "outside-first"})
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(payload))
    shutil.rmtree(tmp_path / "ingestion")

    result, _ = _run(
        tmp_path,
        folder,
        gold,
        max_questions=1,
        question_selection="first_matched",
        only_question_docs=True,
    )

    assert result.eval is not None
    assert [case.case_id for case in result.eval.cases] == ["page-two"]
    assert result.eval.cases[0].verdict == "pass"
    assert _statuses(result)["Meridian Interim 2024.pdf"] == "published"
    assert _statuses(result)["atlas.pdf"] == "skipped_not_referenced"


def test_the_check_alone_reads_no_pdf_and_writes_nothing(tmp_path: Path) -> None:
    folder = _three(tmp_path)
    questions = _questions(tmp_path, _q("q1", "absent"), _q("q2", "ORION FY2024"))
    if os.name == "posix":
        for pdf in folder.iterdir():
            pdf.chmod(0)
    try:
        check = check_question_docs(
            folder, questions, max_questions=1, question_selection="first_matched"
        )
    finally:
        for pdf in folder.iterdir():
            pdf.chmod(0o644)
    assert check is not None and check.selection is not None
    assert [item.question_id for item in check.selection.selected] == ["q2"]
    table = describe(check)
    assert "'ORION FY2024' → orion_fy2024.pdf  [normalized]" in table
    assert "跳过 q1: ['absent']" in table
    assert sorted(path.name for path in tmp_path.iterdir()) == ["pdfs", "questions.jsonl"]
