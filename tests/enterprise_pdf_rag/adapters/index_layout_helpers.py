"""A synthetic long report: a running header and page number on every page, narrative pages,
and a long unruled statement with a two-line header (index-text layout tests). Fictional."""

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    _extent,
    _inside,
    _region,
    _reply,
)
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import LLM_ENV
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PAGE = (420.0, 600.0)
PAGES = 13
RUNNING_HEADER = "Acme Insurance Group Interim Report 2024"
STATEMENT_TITLE = "Consolidated income statement"
# The stub layout's Table region on the statement page: every statement span lies inside.
STATEMENT_BBOX = (15.0, 85.0, 405.0, 545.0)
# Top / bottom margin bands the stub layout cuts the running lines from.
HEADER_LIMIT = 40.0
FOOTER_LIMIT = 560.0
STATEMENT_HEAD = ("For the six months ended 30 June", ("US$m", "2024", "2023"))
_ITEMS = (
    "Claims incurred",
    "Reinsurance result",
    "Underwriting result",
    "Interest income",
    "Net investment income",
    "Finance expenses on contracts",
    "Reinsurance finance income",
    "Net financial result",
    "Fee income",
    "Commission expenses",
    "Operating expenses",
    "Finance costs",
    "Share of associates",
    "Other gains",
    "Other losses",
    "Foreign exchange result",
    "Insurance revenue",
    "Profit before tax",
    "Tax expense",
    "Profit for the period",
    "Attributable to shareholders",
    "Attributable to minorities",
    "Basic earnings per share",
    "Diluted earnings per share",
    "Dividends declared",
    "Retained earnings movement",
    "Other comprehensive income",
    "Fair value reserve change",
    "Cash flow hedges",
    "Translation differences",
    "Total comprehensive income",
    "Comprehensive income to shareholders",
)
# Each item prints two figures; the asked one, ``Insurance revenue 2024``, is 12,345.
STATEMENT_ROWS = tuple(
    (
        item,
        f"{12345 + 7 * index:,}" if item != "Insurance revenue" else "12,345",
        f"{9000 + index:,}",
    )
    for index, item in enumerate(_ITEMS)
)
QUESTION = "Insurance revenue 2024"
ANSWER = "12,345"
NARRATIVE = (
    "Insurance revenue grew across the group's markets in 2024.",
    "Management reviewed new business, pricing and retention with each",
    "regional team and expects the momentum to continue next year.",
)


def _font(page: pdfspine.Page) -> str:
    cast(_FontInsertionPage, page).insert_font(
        fontname="Authored",
        fontbuffer=(_FIXTURES / "authored-donut-ascii.ttf").read_bytes(),
    )
    return "Authored"


def report_pdf(path: Path, *, pages: int = PAGES) -> Path:
    """``pages`` pages: narrative on every page but the last, the statement on the last."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pdfspine.open() as document:
        for number in range(pages):
            page = document.new_page(width=PAGE[0], height=PAGE[1])
            font = _font(page)
            page.insert_text((20, 20), RUNNING_HEADER, fontsize=8, fontname=font)
            page.insert_text((205, 590), str(number + 1), fontsize=8, fontname=font)
            if number != pages - 1:
                page.insert_text(
                    (20, 64), f"Operating review part {number + 1}", fontsize=14, fontname=font
                )
                for line, text in enumerate(NARRATIVE):
                    page.insert_text((20, 100 + 16 * line), text, fontsize=9, fontname=font)
                continue
            page.insert_text((20, 64), STATEMENT_TITLE, fontsize=14, fontname=font)
            caption, columns = STATEMENT_HEAD
            page.insert_text((240, 100), caption, fontsize=7, fontname=font)
            for x, text in zip((240, 300, 360), columns, strict=True):
                page.insert_text((x, 112), text, fontsize=7, fontname=font)
            for index, (item, now, before) in enumerate(STATEMENT_ROWS):
                y = 128 + 12.5 * index
                page.insert_text((22, y), item, fontsize=7, fontname=font)
                page.insert_text((300, y), now, fontsize=7, fontname=font)
                page.insert_text((360, y), before, fontsize=7, fontname=font)
        path.write_bytes(document.tobytes())
    return path


def layout_sender(tasks: Counter[str]) -> Callable[..., bytes]:
    """The offline layout model: the top and bottom margin lines are a Text region each, the
    statement a Table region, everything else one Text region. Nothing else is answered."""

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        content = json.loads(payload)["messages"][1]["content"]
        assert isinstance(content, list), "lite sends only the page layout to the model"
        tasks["page-layout"] += 1
        prompt = content[0]["text"]
        observations: list[dict[str, Any]] = json.loads(
            prompt.split("Source text observations:\n", 1)[1]
        )
        statement = any(item["text"] == STATEMENT_TITLE for item in observations)
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in observations:
            bbox = [float(value) for value in item["bbox"]]
            name = (
                "header"
                if bbox[3] <= HEADER_LIMIT
                else "footer"
                if bbox[1] >= FOOTER_LIMIT
                else "table"
                if statement and _inside(item, STATEMENT_BBOX)
                else "body"
            )
            groups.setdefault(name, []).append(item)
        regions = [
            _region(
                name,
                "Table" if name == "table" else "Text",
                _extent(owned),
                [str(item["id"]) for item in owned],
            )
            for name, owned in groups.items()
        ]
        return _reply({"regions": regions, "unassigned_span_ids": [], "diagnostics": []})

    return sender


def report_env(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)
    tasks: Counter[str] = Counter()
    monkeypatch.setattr(
        "ragspine.common.evidence.providers.json_completion._send_once", layout_sender(tasks)
    )
    return tasks


def row_script(prompt: str) -> ModelAnswer:
    """Answer only from an evidence row that prints the asked item; otherwise decline."""
    for block in prompt.split("| member ")[1:]:
        member_id = block[:64]
        for line in block.splitlines()[1:]:
            if (
                line.startswith("fragments.row-")
                and "\tInsurance revenue\t" in f"\t{line.split(': ', 1)[1]}"
            ):
                path = line.split(": ", 1)[0]
                claim = ModelClaim(
                    claim_id="c1",
                    member_id=member_id,
                    kind="quote",
                    field_path=path,
                    text=f"Insurance revenue\t{ANSWER}",
                )
                return answered(ANSWER, claim)
    return declined()


def write_question(tmp_path: Path) -> Path:
    path = tmp_path / "questions.jsonl"
    path.write_text(
        json.dumps({"id": "q1", "question": QUESTION, "doc": "report.pdf", "expected": ANSWER})
        + "\n"
    )
    return path


def run_report(
    tmp_path: Path, *, answers: str, **options: object
) -> tuple[FolderPipelineResult, list[str]]:
    """``run_folder_pipeline`` over ``tmp_path / "pdfs"`` with an offline answer script."""
    llm, prompts = scripted_client(tmp_path / answers, row_script, max_live_calls=5)
    result = run_folder_pipeline(
        tmp_path / "pdfs",
        questions=write_question(tmp_path),
        ingestion_root=tmp_path / "ingestion",
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        build_tree=False,
        **options,  # type: ignore[arg-type]
    )
    return result, prompts
