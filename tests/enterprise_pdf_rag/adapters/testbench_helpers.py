"""An offline folder run whose questions land in every retrieval test bench diagnosis.

``bench_pdf`` authors the mixed report's prose, ruled table, unruled table and chart
pages, then a run of filler pages that out-score one buried dividend page on BM25. The
question set asks one question per diagnosis; ``answer_script`` answers the ones the lite
helpers know how to cite, declines the one meant to abstain and everything it cannot cite.
"""

import json
from pathlib import Path

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.answers.prompt import ModelAnswer
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    QUESTIONS,
    RUNNING_HEADER,
    TITLE,
    UNRULED_ROWS_TABLE,
    _draw_chart,
    _font,
    claim_script,
    lite_env,
)
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import DEFAULT_TABLE, _draw_table
from tests.enterprise_pdf_rag.answers.fake_llm import declined, scripted_client

FILLER_PAGES = 12
# 1-based: prose, ruled, unruled, chart, then the fillers, then the buried dividend page.
DIVIDEND_PAGE = 4 + FILLER_PAGES + 1
DECLINED = frozenset({"What was Net profit in 1H26?"})
# id → question, doc, pages, expected; one question per diagnosis the bench reports.
BENCH_QUESTIONS: dict[str, dict[str, object]] = {
    "correct": {"question": "What were Sales in 2025?", "pages": 4, "expected": "150"},
    "wrong": {"question": "What is the Revenue value?", "pages": 2, "expected": "9,999"},
    "abstained": {"question": "What was Net profit in 1H26?", "pages": 3, "expected": "567"},
    "buried": {"question": "dividend payout", "pages": DIVIDEND_PAGE, "expected": "35 cents"},
    "missed": {"question": "Sales 2024", "pages": 1, "expected": "999"},
    "unrouted": {"question": "What was revenue?", "pages": 1, "doc": "missing.pdf"},
    "no_pages": {"question": "What was the net margin in 1H26?", "expected": "12%"},
    "bare": {"question": "How is ROE defined?"},
}


def bench_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pdfspine.open() as document:
        for number, kind in enumerate(("text", "ruled", "unruled", "chart")):
            page = document.new_page(width=240, height=160)
            fontname = _font(page)
            page.insert_text((20, 14), RUNNING_HEADER, fontsize=7, fontname=fontname)
            if kind == "text":
                page.insert_text((20, 40), TITLE, fontsize=12, fontname=fontname)
                page.insert_text(
                    (20, 70), "Revenue grew in Hong Kong.", fontsize=9, fontname=fontname
                )
                page.insert_text(
                    (20, 90), "Net margin was 12% in 1H26.", fontsize=9, fontname=fontname
                )
                continue
            page.insert_text(
                (20, 36), f"Section {number + 1} {kind}", fontsize=11, fontname=fontname
            )
            if kind == "ruled":
                _draw_table(page, fontname, DEFAULT_TABLE)
            elif kind == "unruled":
                _draw_table(page, fontname, UNRULED_ROWS_TABLE)
            else:
                _draw_chart(page, fontname)
        for number in range(FILLER_PAGES):
            page = document.new_page(width=240, height=160)
            fontname = _font(page)
            page.insert_text((20, 14), RUNNING_HEADER, fontsize=7, fontname=fontname)
            page.insert_text(
                (20, 40), f"Dividend payout note {number + 1}", fontsize=11, fontname=fontname
            )
            page.insert_text((20, 70), "The dividend payout review.", fontsize=9, fontname=fontname)
        page = document.new_page(width=240, height=160)
        fontname = _font(page)
        page.insert_text((20, 14), RUNNING_HEADER, fontsize=7, fontname=fontname)
        page.insert_text((20, 40), "Shareholder returns", fontsize=11, fontname=fontname)
        for line, text in enumerate(
            (
                "Cash went back to holders",
                "with a dividend of 35 cents",
                "as the board declared,",
                "beside buybacks and other",
                "capital steps in the half.",
            )
        ):
            page.insert_text((20, 64 + 14 * line), text, fontsize=9, fontname=fontname)
        path.write_bytes(document.tobytes())
    return path


def answer_script(prompt: str) -> ModelAnswer:
    question = prompt.split("\n", 2)[1]
    if question in DECLINED or not any(item[1] == question for item in QUESTIONS):
        return declined()
    return claim_script(prompt)


def write_bench_questions(path: Path) -> Path:
    lines = []
    for case_id, fields in BENCH_QUESTIONS.items():
        record = {"id": case_id, "doc": "bench.pdf", **fields}
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def bench_run(root: Path, monkeypatch: pytest.MonkeyPatch) -> FolderPipelineResult:
    """Ingest the bench report in lite mode and answer every bench question offline."""
    lite_env(monkeypatch)
    bench_pdf(root / "pdfs" / "bench.pdf")
    llm, _ = scripted_client(root / "answers", answer_script, max_live_calls=50)
    return run_folder_pipeline(
        root / "pdfs",
        questions=write_bench_questions(root / "questions.jsonl"),
        ingestion_root=root / "ingestion",
        max_live_calls_per_pdf=400,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        ingest_mode="lite",
        report_dir=root / "reports",
    )
