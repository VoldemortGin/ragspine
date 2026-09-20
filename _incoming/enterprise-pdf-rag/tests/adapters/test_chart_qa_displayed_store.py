"""Pinned storage is verified before the dedicated bar source port runs."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from tests.adapters.test_chart_qa_store import published_chart

from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.cli import main
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DisplayedRefusal,
    DisplayedRefusalReason,
)
from enterprise_pdf_rag.figures.chart_qa.models import ChartQueryError, QueryFailure


def test_old_publication_is_not_a_bar_and_bad_pins_still_conflict(
    tmp_path: Path,
) -> None:
    sources, outputs, pin = published_chart(tmp_path, labels=True)
    resolver = StoredDisplayResolver(sources, outputs, processing_id=pin.processing_id)
    with pytest.raises(DisplayedRefusal) as refused:
        resolver.resolve(pin)
    assert refused.value.reason is DisplayedRefusalReason.UNQUALIFIED_MEMBER
    for changed in (
        replace(pin, processing_id="f" * 64),
        replace(pin, snapshot_id="f" * 64),
        replace(pin, member_id="f" * 64),
    ):
        with pytest.raises(ChartQueryError) as caught:
            resolver.resolve(changed)
        assert caught.value.code is QueryFailure.PIN_CONFLICT


def test_cli_parses_explicit_v2_and_checks_its_pinned_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources, outputs, pin = published_chart(tmp_path, labels=True)
    request = tmp_path / "v2.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "chart-qa-v2",
                "kind": "chart",
                "operation": "lookup",
                "processing_id": pin.processing_id,
                "snapshot_id": "f" * 64,
                "member_id": pin.member_id,
                "series": "Expense Ratio",
                "period": "1H24",
                "unit": "%",
                "points": [{"point_id": "point-1h24", "category": "1H24"}],
            }
        )
    )
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "chart-qa",
                "--request",
                str(request),
                "--source-store",
                str(sources.root),
                "--processing-store",
                str(outputs.root),
            ]
        )
    assert caught.value.code == 2
    assert "Query names another retrieval snapshot" in capsys.readouterr().err
