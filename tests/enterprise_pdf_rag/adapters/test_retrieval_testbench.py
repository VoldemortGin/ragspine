"""The retrieval test bench reads one offline folder run back as one diagnosis per question."""

import csv
import io
import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters import retrieval_testbench
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.retrieval_testbench import (
    COLUMNS,
    DIAGNOSES,
    INF,
    NA,
    format_table,
    run_retrieval_testbench,
    write_testbench,
)
from enterprise_pdf_rag.cli import main
from ragspine.eval.retrieval_only import gold_rank, recall_ks, retrieval_metrics
from tests.enterprise_pdf_rag.adapters.testbench_helpers import (
    BENCH_QUESTIONS,
    DIVIDEND_PAGE,
    bench_run,
)


@dataclass(frozen=True)
class Bench:
    root: Path
    result: FolderPipelineResult

    @property
    def db(self) -> Path:
        return self.root / "ingestion" / "answers-audit.sqlite"

    @property
    def questions(self) -> Path:
        return self.root / "questions.jsonl"

    @property
    def report(self) -> Path:
        return self.root / "reports" / "report.json"


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bench]:
    root = tmp_path_factory.mktemp("bench")
    with pytest.MonkeyPatch.context() as monkeypatch:
        result = bench_run(root, monkeypatch)
    yield Bench(root, result)


def _ranked(db: Path, question: str) -> list[dict[str, object]]:
    with closing(sqlite3.connect(db)) as connection:
        (raw,) = connection.execute(
            "SELECT ranked FROM answers WHERE question = ? ORDER BY id DESC LIMIT 1", (question,)
        ).fetchone()
    return list(json.loads(raw))


def test_every_question_gets_the_diagnosis_its_failure_calls_for(bench: Bench) -> None:
    result = run_retrieval_testbench(bench.db, bench.questions, report=bench.report)

    diagnoses = {row.question_id: row.diagnosis for row in result.rows}
    assert diagnoses == {
        "correct": "correct",
        "wrong": "in_prompt_wrong",
        "abstained": "in_prompt_abstained",
        "buried": "retrieved_not_in_prompt",
        "missed": "not_retrieved",
        "unrouted": "routing_failed",
        "no_pages": "correct",
        "bare": "unjudged",
    }
    assert [row.question_id for row in result.rows] == list(BENCH_QUESTIONS)
    for row in result.rows:
        assert row.diagnosis_text == DIAGNOSES[row.diagnosis]
    counts = result.summary["diagnoses"]
    assert list(counts) == list(DIAGNOSES)
    assert counts["correct"] == 2 and counts["not_retrieved"] == 1 and sum(counts.values()) == 8


def test_each_channel_seat_is_read_from_the_journalled_ranking(bench: Bench) -> None:
    rows = {
        row.question_id: row
        for row in run_retrieval_testbench(bench.db, bench.questions, report=bench.report).rows
    }

    buried = rows["buried"]
    ranked = _ranked(bench.db, "dividend payout")
    on_page = [entry for entry in ranked if entry["page_index"] == DIVIDEND_PAGE - 1]
    seat = min(int(str(entry["lexical_rank"])) for entry in on_page)
    position = next(i + 1 for i, entry in enumerate(ranked) if entry in on_page)
    assert seat > 10 and buried.bm25_rank == seat and buried.fused_rank == position
    # A BM25-only question ran no vector channel, and this run built no tree.
    assert (buried.vector_rank, buried.tree_rank, buried.ranking) == (NA, NA, "full")
    assert buried.fusion_mode == "bm25_only"
    assert (buried.in_prompt, buried.prompt_rank, buried.status) == (False, INF, "abstained")

    missed = rows["missed"]
    assert (missed.bm25_rank, missed.fused_rank, missed.in_prompt) == (INF, INF, False)

    abstained = rows["abstained"]
    assert abstained.fusion_mode == "rrf" and abstained.in_prompt is True
    ranked = _ranked(bench.db, "What was Net profit in 1H26?")
    on_page = [entry for entry in ranked if entry["page_index"] == 2]
    assert abstained.bm25_rank == min(int(str(entry["lexical_rank"])) for entry in on_page)
    assert abstained.vector_rank == min(int(str(entry["vector_rank"])) for entry in on_page)
    assert abstained.status == "abstained" and abstained.abstain_reason == "model_declined"
    assert abstained.content_hit is False

    correct = rows["correct"]
    assert (correct.cited_pages, correct.cited_page_hit, correct.content_hit) == ("4", True, True)
    assert correct.answer == "The report prints 150." and correct.link == "text+document"
    assert rows["wrong"].content_hit is False and rows["wrong"].in_prompt is True

    no_pages = rows["no_pages"]
    # No pages: no rank is made up; the prompt is checked for the expected text instead.
    assert (no_pages.bm25_rank, no_pages.fused_rank, no_pages.prompt_rank) == (NA, NA, NA)
    assert (no_pages.expected_pages, no_pages.in_prompt, no_pages.cited_page_hit) == (
        NA,
        True,
        NA,
    )

    unrouted = rows["unrouted"]
    assert unrouted.routing_failed is True and "missing.pdf" in unrouted.routing_detail
    assert (unrouted.audit_id, unrouted.status, unrouted.fused_rank) == (NA, NA, NA)


def test_the_metrics_follow_the_retrieval_only_definitions(bench: Bench) -> None:
    result = run_retrieval_testbench(bench.db, bench.questions, report=bench.report)
    assert bench.result.eval is not None

    # The prompt-level metrics are exactly what run-folder's own report computed.
    assert result.summary["metrics"]["prompt"] == bench.result.eval.metrics

    ks = recall_ks(10)
    expected: list[tuple[int | None, int | None]] = []
    for case_id, fields in BENCH_QUESTIONS.items():
        if "pages" not in fields:
            continue
        groups = (frozenset({int(str(fields["pages"]))}),)
        if case_id == "unrouted":  # never retrieved: a routing miss, not a ranking miss
            continue
        hits = [
            ("doc", int(str(entry["page_index"])) + 1)
            for entry in _ranked(bench.db, str(fields["question"]))
        ]
        expected.append((gold_rank(hits, groups), gold_rank(hits, groups, distinct=True)))
    assert result.summary["metrics"]["fused"] == retrieval_metrics(expected, ks)

    channels = result.summary["channels"]
    assert set(channels) == {"bm25", "vector", "tree", "fused"}
    assert channels["tree"]["judged"] == 0
    assert channels["bm25"]["judged"] == 5 and channels["bm25"]["hit"] == 0.8
    assert result.summary["by_doc"]["bench.pdf"]["correct"] == 2
    assert result.summary["by_doc"]["missing.pdf"] == {"routing_failed": 1}
    assert result.summary["n/a"] == {"expected": 2, "pages": 2}


def test_csv_and_json_carry_every_column(bench: Bench, tmp_path: Path) -> None:
    result = run_retrieval_testbench(bench.db, bench.questions, report=bench.report)

    csv_path, json_path = write_testbench(result, tmp_path / "out")

    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert tuple(rows[0]) == COLUMNS and len(rows) == 1 + len(BENCH_QUESTIONS)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert [tuple(row) for row in payload["rows"]] == [COLUMNS] * len(BENCH_QUESTIONS)
    assert payload["summary"] == json.loads(json.dumps(result.summary))
    assert {"question_id", "expected", "answer", "diagnosis", "bm25_rank"} <= set(COLUMNS)
    assert not list((tmp_path / "out").glob("*.partial"))

    table = format_table(result)
    assert "retrieved_not_in_prompt" in table and DIAGNOSES["not_retrieved"] in table


def test_a_journal_without_the_ranking_column_reads_as_not_available(
    bench: Bench, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy.sqlite"
    shutil.copy(bench.db, legacy)
    with closing(sqlite3.connect(legacy)) as connection, connection:
        connection.execute("ALTER TABLE answers DROP COLUMN ranked")

    result = run_retrieval_testbench(legacy, bench.questions, report=bench.report)
    rows = {row.question_id: row for row in result.rows}
    for row in result.rows:
        if row.question_id != "unrouted":
            assert (row.ranking, row.bm25_rank, row.fused_rank) == (NA, NA, NA)
    # Without pages per member only a page window can still prove the page reached the prompt.
    assert rows["buried"].diagnosis == "not_in_prompt"
    assert rows["correct"].diagnosis == "correct"
    assert result.summary["n/a"]["ranked"] == 7
    assert result.summary["n/a"]["member_pages"] == 7
    assert result.summary["metrics"]["fused"] == {}

    mapped = run_retrieval_testbench(
        legacy, bench.questions, report=bench.report, ingestion_root=bench.root / "ingestion"
    )
    rows = {row.question_id: row for row in mapped.rows}
    assert rows["buried"].ranking == "head"
    # The seated head holds no dividend page, and a head cannot say no channel found it.
    assert (rows["buried"].bm25_rank, rows["buried"].diagnosis) == (NA, "not_in_prompt")
    assert rows["abstained"].bm25_rank != NA and rows["abstained"].diagnosis == (
        "in_prompt_abstained"
    )
    # Prompt seats are known again once member pages are, and match run-folder's report.
    assert bench.result.eval is not None
    assert mapped.summary["metrics"]["prompt"] == bench.result.eval.metrics
    assert result.summary["metrics"]["prompt"]["judged"] == 1  # only the unrouted question
    assert mapped.summary["n/a"]["ranked"] == 7 and "member_pages" not in mapped.summary["n/a"]


def test_the_latest_journal_row_of_a_question_wins(bench: Bench, tmp_path: Path) -> None:
    copy = tmp_path / "journal.sqlite"
    shutil.copy(bench.db, copy)
    with closing(sqlite3.connect(copy)) as connection, connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(answers)")]
        kept = ", ".join(name for name in columns if name != "id")
        connection.execute(
            f"INSERT INTO answers ({kept}) SELECT {kept} FROM answers WHERE question = ?",
            ("What was Net profit in 1H26?",),
        )
        connection.execute(
            "UPDATE answers SET status = 'answered', answer_text = 'It was 567.', "
            "abstain_reason = NULL WHERE id = (SELECT MAX(id) FROM answers)"
        )

    rows = {
        row.question_id: row
        for row in run_retrieval_testbench(copy, bench.questions, report=bench.report).rows
    }
    assert rows["abstained"].diagnosis == "correct"
    assert rows["abstained"].audit_id == max(
        int(str(row.audit_id)) for row in rows.values() if row.audit_id != NA
    )


def test_selection_and_routing_without_a_report(bench: Bench) -> None:
    first = run_retrieval_testbench(bench.db, bench.questions, max_questions=3)
    assert [row.question_id for row in first.rows] == ["correct", "wrong", "abstained"]

    chosen = run_retrieval_testbench(bench.db, bench.questions, question_ids=["unrouted", "missed"])
    assert [row.question_id for row in chosen.rows] == ["missed", "unrouted"]
    rows = {row.question_id: row for row in chosen.rows}
    # Without a report or a folder nothing says the question went unrouted: no record, n/a.
    assert (rows["unrouted"].diagnosis, rows["unrouted"].routing_failed) == ("no_record", NA)
    assert rows["missed"].link == "text" and rows["missed"].diagnosis == "not_retrieved"

    routed = run_retrieval_testbench(
        bench.db, bench.questions, folder=bench.root / "pdfs", question_ids=["unrouted"]
    )
    (row,) = routed.rows
    assert row.diagnosis == "routing_failed" and "missing.pdf" in row.routing_detail

    with pytest.raises(ValueError, match="nope"):
        run_retrieval_testbench(bench.db, bench.questions, question_ids=["nope"])
    with pytest.raises(FileNotFoundError):
        run_retrieval_testbench(bench.root / "absent.sqlite", bench.questions)


def test_the_bench_logs_nothing(bench: Bench, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        run_retrieval_testbench(bench.db, bench.questions, report=bench.report)
    assert caplog.records == []


def test_the_audit_command_prints_and_writes_the_bench(
    bench: Bench,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    common = ["audit", "--testbench", "--question-set", str(bench.questions), "--db", str(bench.db)]
    common += ["--report", str(bench.report)]

    assert main([*common, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["diagnosis"] for row in payload["rows"]][:2] == ["correct", "in_prompt_wrong"]

    assert main([*common, "--format", "csv", "--max-questions", "2"]) == 0
    lines = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert tuple(lines[0]) == COLUMNS and len(lines) == 3

    assert main([*common, "--question-id", "buried"]) == 0
    printed = capsys.readouterr().out
    assert "buried" in printed and "retrieved_not_in_prompt" in printed

    # --write lands in ROOT_DIR/data/reports/<question-set stem>, and nowhere outside data/.
    monkeypatch.setattr(retrieval_testbench, "ROOT_DIR", tmp_path)
    assert main([*common, "--write"]) == 0
    target = tmp_path / "data" / "reports" / "questions"
    assert (target / "testbench.csv").is_file() and (target / "testbench.json").is_file()
    assert "testbench.csv" in capsys.readouterr().out
    assert main([*common, "--out", str(tmp_path / "elsewhere")]) == 1
    assert "data" in capsys.readouterr().out and not (tmp_path / "elsewhere").exists()

    assert main(["audit", "--testbench", "--db", str(bench.db)]) == 1
    assert "--question-set" in capsys.readouterr().out
    missing = ["audit", "--testbench", "--question-set", str(bench.questions)]
    assert main([*missing, "--db", str(tmp_path / "none.sqlite")]) == 1
    assert "no journal" in capsys.readouterr().out
