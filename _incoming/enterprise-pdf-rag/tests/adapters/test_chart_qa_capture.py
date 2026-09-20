"""The capture runner calls only explicit loopback ChartQA endpoints."""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.adapters.test_chart_qa_evaluation import _perfect_observations

from enterprise_pdf_rag.adapters.chart_qa_capture import (
    CaptureTarget,
    capture_chart_qa,
)
from enterprise_pdf_rag.adapters.chart_qa_evaluation import load_gold

ROOT = Path(__file__).parents[2]
GOLD_PATH = ROOT / "benchmarks/aia-2026-interim/chart-qa-gold-v1.json"


def _targets() -> bytes:
    gold = load_gold(GOLD_PATH.read_bytes())
    return json.dumps(
        {
            "schema_version": "chart-qa-capture-targets-v1",
            "targets": {
                target: {
                    "endpoint": "http://127.0.0.1:18766",
                    "processing_id": "1" * 64,
                    "snapshot_id": "2" * 64,
                    "member_id": "3" * 64,
                }
                for target in {case.request.target for case in gold.cases}
            },
        },
        separators=(",", ":"),
    ).encode()


def test_capture_uses_public_requests_and_records_actual_responses() -> None:
    gold_payload = GOLD_PATH.read_bytes()
    gold = load_gold(gold_payload)
    expected = _perfect_observations(gold)["results"]
    pending: list[dict[str, Any]] = list(expected)
    requests: list[dict[str, Any]] = []

    def transport(target: CaptureTarget, payload: bytes) -> tuple[int, bytes]:
        assert target.endpoint == "http://127.0.0.1:18766"
        request = json.loads(payload)
        requests.append(request)
        result = pending.pop(0)
        if result["body"] is not None:
            return result["http_status"], json.dumps(result["body"]).encode()
        if result["http_status"] == 422:
            return 422, b'{"detail":[]}'
        return result["http_status"], json.dumps(
            {
                "error": {
                    "code": result["error_class"],
                    "message": "bounded test response",
                }
            }
        ).encode()

    captured = capture_chart_qa(
        gold_payload, _targets(), transport=transport
    ).model_dump(mode="json")

    assert not pending
    assert captured == {
        "schema_version": "chart-qa-observations-v1",
        "results": expected,
    }
    assert all(request["kind"] == "chart" for request in requests)
    assert all("value" not in request for request in requests)
    assert all("value_kind" not in request for request in requests)


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://127.0.0.1:8766",
        "http://example.com:8766",
        "http://127.0.0.1:8766/path",
        "http://127.0.0.1",
    ),
)
def test_capture_target_rejects_nonloopback_or_ambiguous_endpoints(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="loopback"):
        CaptureTarget(
            endpoint=endpoint,
            processing_id="1" * 64,
            snapshot_id="2" * 64,
            member_id="3" * 64,
        )


def test_capture_requires_exact_target_coverage() -> None:
    payload = json.loads(_targets())
    payload["targets"].pop(next(iter(payload["targets"])))

    with pytest.raises(ValueError, match="exactly cover"):
        capture_chart_qa(
            GOLD_PATH.read_bytes(),
            json.dumps(payload).encode(),
            transport=lambda _target, _payload: (500, b"{}"),
        )
