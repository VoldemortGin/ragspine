"""Authored table pages the ruling proof cannot see a grid in (ADR 0027): synthetic only."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from ragspine.common.evidence.settings import ROOT_DIR
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    PROVIDER_BASE_URL,
    text_partition_sender,
)
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import metadata_reply
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage

PAGE_SIZE = (400.0, 240.0)
# The grid the stub layout calls a Table: every authored table span lies inside it.
STATEMENT_BBOX = (18.0, 48.0, 382.0, 212.0)
TITLE = "Acme statement page"
# (x, baseline, text, font size): a borderless income statement — a two-line column header,
# an indented sub-item, accounting negatives, a label wrapped over two lines, a raised
# footnote marker beside a value set 0.6pt off the baseline, a total and a footnote line.
STATEMENT = (
    (250.0, 62.0, "Year ended 31 December", 9.0),
    (262.0, 76.0, "2024", 9.0),
    (330.0, 76.0, "2023", 9.0),
    (22.0, 94.0, "Revenue", 9.0),
    (250.0, 94.0, "1,234,567", 9.0),
    (318.0, 94.0, "1,100,200", 9.0),
    (34.0, 110.0, "Cost of sales", 9.0),
    (256.0, 110.0, "(456,789)", 9.0),
    (324.0, 110.0, "(400,100)", 9.0),
    (22.0, 126.0, "Other operating income and", 9.0),
    (22.0, 140.0, "expenses", 9.0),
    (268.0, 140.0, "12,345", 9.0),
    (330.0, 140.0, "(9,876)", 9.0),
    (22.0, 158.0, "Operating profit", 9.0),
    (104.0, 154.0, "1", 5.0),
    (262.0, 158.6, "790,123", 9.0),
    (324.0, 158.0, "690,224", 9.0),
    (22.0, 178.0, "Total", 9.0),
    (262.0, 178.0, "790,123", 9.0),
    (324.0, 178.0, "690,224", 9.0),
    (22.0, 198.0, "1 Restated.", 7.0),
)
# The rows the statement must transcribe to, cells joined by a tab, top down.
STATEMENT_ROWS = (
    "Year ended 31 December",
    "2024\t2023",
    "Revenue\t1,234,567\t1,100,200",
    "Cost of sales\t(456,789)\t(400,100)",
    "Other operating income and",
    "expenses\t12,345\t(9,876)",
    "Operating profit\t1\t790,123\t690,224",
    "Total\t790,123\t690,224",
    "1 Restated.",
)
# Ordinary prose the layout mislabels as a table.
PARAGRAPH = (
    (22.0, 70.0, "The group reported steady growth in", 10.0),
    (22.0, 86.0, "revenue during the period under", 10.0),
    (22.0, 102.0, "review, driven by new customers.", 10.0),
)


def statement_pdf(
    path: Path,
    *,
    frame: bool = False,
    lines: tuple[tuple[float, float, str, float], ...] = STATEMENT,
    page_count: int = 2,
) -> Path:
    """``page_count`` pages; the last carries ``lines`` inside ``STATEMENT_BBOX``.

    ``frame`` draws the outer box only (no interior rule), which the ``lines`` detector
    finds no table in either.
    """
    with pdfspine.open() as document:
        for number in range(page_count):
            page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
            cast(_FontInsertionPage, page).insert_font(
                fontname="Authored",
                fontbuffer=(
                    ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf"
                ).read_bytes(),
            )
            page.insert_text((20, 30), f"{TITLE} {number + 1}", fontsize=12, fontname="Authored")
            if number != page_count - 1:
                continue
            if frame:
                page.draw_rect(STATEMENT_BBOX, width=1)
            for x, y, text, size in lines:
                page.insert_text((x, y), text, fontsize=size, fontname="Authored")
        path.write_bytes(document.tobytes())
    return path


def model_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    table_bbox: tuple[float, float, float, float] = STATEMENT_BBOX,
) -> list[bytes]:
    """Offline layout + page-metadata transport: ``table_bbox`` is one Table region."""
    for key, value in {
        "APP_LLM_API_KEY": "offline-secret",
        "APP_LLM_BASE_URL": PROVIDER_BASE_URL,
        "APP_LLM_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    calls: list[bytes] = []
    layout = text_partition_sender(calls, table_bbox=table_bbox)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        content = json.loads(payload)["messages"][1]["content"]
        if isinstance(content, list):
            return layout(url, api_key=api_key, payload=payload, timeout=timeout)
        calls.append(payload)
        reply = metadata_reply(content, fabricate=False)
        return json.dumps(
            {"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        ).encode()

    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", sender)
    return calls


def ingest(
    pdf: Path, root: Path, *, rows: bool | None, max_live_calls: int = 10
) -> IngestionSummary:
    """``rows=None`` leaves the parameter out entirely (the default call)."""
    extra: dict[str, bool] = {} if rows is None else {"unverified_tables_as_rows": rows}
    call: Callable[..., IngestionSummary] = ingest_pdf
    return call(pdf=pdf, stage="semantics", max_live_calls=max_live_calls, output_dir=root, **extra)
