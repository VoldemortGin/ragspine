"""run-folder answers every question across every published PDF (ADR 0032).

Four authored PDFs: two reports, and two that print the same heading with different
figures. The scripted model quotes the line a question asks for from whichever block holds
it — or, once, deliberately from the block of the other document — so every assertion is
about where the evidence really was.
"""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.retrieval_testbench import NA, run_retrieval_testbench
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _OFFLINE,
    _ORION,
    _PER_PDF,
    _model_env,
    _pdf,
)
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client

_LOW = "Revenue summary 100"
_HIGH = "Revenue summary 200"
_ASKED = re.compile(r"what does (.+) say on page (\d)", re.IGNORECASE)
_FRAGMENT = re.compile(r"^fragments\.(\S+): (.*)$", re.MULTILINE)
_QUESTIONS: tuple[dict[str, object], ...] = (
    {
        "id": "named",
        "question": f"What does {_MERIDIAN} say on page 2?",
        "doc": "meridian.pdf",
        "pages": "2",
        "expected": f"{_MERIDIAN} page 2",
    },
    {
        "id": "misspelt",
        "question": f"What does {_ORION} say on page 3?",
        "doc": "orion_fy2024_annual.pdf",
        "pages": "3",
        "expected": f"{_ORION} page 3",
    },
    {"id": "no-doc", "question": f"What does {_MERIDIAN} say on page 3?"},
    {
        "id": "nowhere",
        "question": "What does Atlas FY2025 Japan say on page 2?",
        "expected": "Atlas FY2025 Japan page 2",
    },
    {"id": "same-heading", "question": f"What does {_HIGH} say on page 1?", "doc": "beta.pdf"},
    {
        "id": "cross-cited",
        "question": f"Cross-cite: what does {_HIGH} say on page 1?",
        "doc": "beta.pdf",
    },
)


def _blocks(prompt: str) -> list[tuple[str, str, str]]:
    """(member id, span id, text) of every fragment line offered in the prompt."""
    found = []
    for block in prompt.split("| member ")[1:]:
        for span_id, text in _FRAGMENT.findall(block):
            found.append((block[:64], span_id, text))
    return found


def _script(prompt: str) -> ModelAnswer:
    question = prompt.split("\n", 2)[1]
    asked = _ASKED.search(question)
    if asked is None:
        return declined()
    needle = f"{asked.group(1)} page {asked.group(2)}"
    # The adversarial case quotes the figure asked for but cites the other document's block.
    source = needle.replace("200", "100") if question.startswith("Cross-cite") else needle
    hit = next(((m, s, t) for m, s, t in _blocks(prompt) if source in t), None)
    if hit is None:
        return declined()
    member_id, span_id, _ = hit
    return answered(
        f"It reads: {needle}",
        ModelClaim(
            claim_id="q",
            member_id=member_id,
            kind="quote",
            field_path=f"fragments.{span_id}",
            text=needle,
        ),
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, FolderPipelineResult, list[str]]:
    root = tmp_path_factory.mktemp("cross")
    folder = root / "pdfs"
    with pytest.MonkeyPatch.context() as monkeypatch:
        _model_env(monkeypatch)
        for name, label in (
            ("meridian.pdf", _MERIDIAN),
            ("orion.pdf", _ORION),
            ("alpha.pdf", _LOW),
            ("beta.pdf", _HIGH),
        ):
            _pdf(folder / name, label)
        questions = root / "questions.jsonl"
        questions.write_text("\n".join(json.dumps(row) for row in _QUESTIONS) + "\n")
        llm, prompts = scripted_client(root / "answers", _script, max_live_calls=20)
        result = run_folder_pipeline(
            folder,
            questions=questions,
            ingestion_root=root / "ingestion",
            max_live_calls_per_pdf=_PER_PDF,
            build_tree=False,
            embedder=_OFFLINE,
            answer_llm=llm,
            report_dir=root / "reports",
        )
    return root, result, prompts


def _shas(result: FolderPipelineResult) -> dict[str, str]:
    return {Path(item.pdf_path).name: str(item.sha256) for item in result.documents}


def test_every_question_is_answered_across_every_pdf_from_the_pdf_holding_it(
    run: tuple[Path, FolderPipelineResult, list[str]],
) -> None:
    _, result, prompts = run
    shas = _shas(result)
    assert result.eval is not None
    cases = {case.case_id: case for case in result.eval.cases}
    assert {case.routing for case in cases.values()} == {"cross_document"}
    assert {case.searched_documents for case in cases.values()} == {4}
    assert result.eval.totals["routing_failed"] == 0

    # (a) a matching doc: answered, cited there, and the label says so.
    named = cases["named"]
    assert (named.verdict, named.cited_documents) == ("answered", (shas["meridian.pdf"],))
    assert (named.expected_doc, named.cited_doc_hit) == (shas["meridian.pdf"], True)
    assert named.page_rank is not None and named.failures == ()
    # (b) a doc a few letters off: still answered, from the right PDF; nothing to label.
    misspelt = cases["misspelt"]
    assert misspelt.verdict == "answered" and misspelt.failures == ()
    assert misspelt.cited_documents == (shas["orion.pdf"],)
    assert (misspelt.expected_doc, misspelt.cited_doc_hit, misspelt.page_rank) == (None,) * 3
    assert misspelt.answer is not None and f"{_ORION} page 3" in misspelt.answer
    # (c) no doc at all: the same.
    no_doc = cases["no-doc"]
    assert no_doc.verdict == "answered" and no_doc.cited_documents == (shas["meridian.pdf"],)
    assert no_doc.answer is not None and f"{_MERIDIAN} page 3" in no_doc.answer
    # (d) in no PDF: an abstention, not a routing failure and not a made-up answer.
    nowhere = cases["nowhere"]
    assert (nowhere.verdict, nowhere.claim_count, nowhere.cited_documents) == ("abstained", 0, ())
    assert nowhere.answer is not None and "Atlas FY2025 Japan page 2" not in nowhere.answer
    # (e) the same heading in two PDFs: the figure is cited to the PDF printing it ...
    same = cases["same-heading"]
    assert same.verdict == "answered" and same.cited_documents == (shas["beta.pdf"],)
    assert same.cited_doc_hit is True
    (claim,) = same.envelope["claims"]
    (citation,) = claim["citations"]
    assert citation["document_sha256"] == shas["beta.pdf"]
    assert citation["quote"] == f"{_HIGH} page 1"
    # ... and claiming it from the other PDF's identical-looking block never verifies.
    crossed = cases["cross-cited"]
    assert (crossed.verdict, crossed.cited_documents) == ("abstained", ())
    (rejected,) = crossed.envelope["rejected"]
    assert rejected["reason"] == "claim_not_in_evidence"
    # Both same-heading blocks reached that prompt, each naming its own PDF (cover title and
    # short sha256).
    cross_prompt = next(prompt for prompt in prompts if prompt.startswith("Question:\nCross-cite"))
    for name in ("alpha.pdf", "beta.pdf"):
        assert re.search(
            rf"document=Revenue summary \d+ page 1 \({shas[name][:12]}\)", cross_prompt
        )
    # One synthesis call per question, no more.
    assert all(case.llm_live_calls <= 1 for case in cases.values())
    assert len(prompts) == len(_QUESTIONS)


def test_the_report_says_the_scope_and_the_journal_names_each_ranked_document(
    run: tuple[Path, FolderPipelineResult, list[str]],
) -> None:
    root, result, _ = run
    shas = set(_shas(result).values())
    markdown = (root / "reports" / "report.md").read_text(encoding="utf-8")
    assert "retrieval scope: every published document of this run" in markdown
    saved = FolderPipelineResult.model_validate_json(
        (root / "reports" / "report.json").read_text(encoding="utf-8")
    )
    assert saved.eval is not None and saved.eval.cases[0].expected_doc is not None

    with closing(sqlite3.connect(root / "ingestion" / "answers-audit.sqlite")) as connection:
        rows = connection.execute("SELECT ranked, searched_documents FROM answers").fetchall()
    assert len(rows) == len(_QUESTIONS)
    for ranked_json, searched_json in rows:
        assert set(json.loads(searched_json)) == shas
        ranked = json.loads(ranked_json)
        assert {entry["document_sha256"] for entry in ranked} <= shas
    assert any(len({entry["document_sha256"] for entry in json.loads(r)}) > 1 for r, _ in rows)


def test_the_bench_judges_pages_inside_the_expected_pdf_only(
    run: tuple[Path, FolderPipelineResult, list[str]],
) -> None:
    root, result, _ = run
    shas = _shas(result)
    bench = run_retrieval_testbench(
        root / "ingestion" / "answers-audit.sqlite",
        root / "questions.jsonl",
        report=root / "reports" / "report.json",
    )
    rows = {row.question_id: row for row in bench.rows}
    assert {row.cross_document for row in rows.values()} == {True}
    assert "routing_failed" not in {row.diagnosis for row in rows.values()}

    named = rows["named"]
    assert (named.expected_doc, named.cited_doc_hit, named.diagnosis) == (
        shas["meridian.pdf"],
        True,
        "correct",
    )
    assert isinstance(named.fused_rank, int) and named.in_prompt is True
    # Page 3 of an unresolved doc names no page across four PDFs: no rank is made up.
    misspelt = rows["misspelt"]
    assert (misspelt.expected_doc, misspelt.fused_rank, misspelt.cited_doc_hit) == (NA, NA, NA)
    assert misspelt.diagnosis == "correct"  # judged on the expected answer text instead
    assert rows["nowhere"].diagnosis in {"in_prompt_abstained", "not_in_prompt"}
    assert rows["same-heading"].cited_doc_hit is True
    assert bench.summary["n/a"]["expected_doc"] == 1
