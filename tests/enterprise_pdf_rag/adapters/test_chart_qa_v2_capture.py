"""Capture records real transport bytes and independent release bindings."""

import json
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.chart_qa_capture import CaptureTarget
from enterprise_pdf_rag.adapters.chart_qa_v2_capture import capture_bar_chart_qa
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation import evaluate_bar_chart_qa
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import load_bar_gold
from tests.enterprise_pdf_rag.adapters.chart_qa_v2_fixtures import (
    observations_fixture,
    targets_fixture,
)

GOLD_PATH = (
    Path(__file__).parents[3]
    / "data/benchmarks/enterprise-pdf-rag/aia-2026-interim/chart-qa-bar-gold-v1.json"
)


def test_capture_preserves_raw_http_and_never_sends_expected_values() -> None:
    payload = GOLD_PATH.read_bytes()
    gold = load_bar_gold(payload)
    targets = targets_fixture(gold)
    responses = observations_fixture(gold, targets)
    calls: list[bytes] = []

    def send(target: CaptureTarget, request: bytes) -> tuple[int, bytes]:
        assert target.endpoint == "http://127.0.0.1:18766"
        original = responses.results[len(calls)]
        assert json.loads(request) == json.loads(original.request_json)
        assert '"value"' not in request.decode()
        assert '"verified"' not in request.decode()
        calls.append(request)
        return original.http_status, original.response_json.encode()

    captured = capture_bar_chart_qa(payload, targets.model_dump_json().encode(), transport=send)
    assert captured == responses
    assert len(calls) == len(gold.cases)
    assert evaluate_bar_chart_qa(
        payload, targets.model_dump_json().encode(), captured.model_dump_json().encode()
    ).passed


def test_remote_target_is_rejected_before_any_transport() -> None:
    payload = GOLD_PATH.read_bytes()
    targets = targets_fixture(load_bar_gold(payload))
    calls: list[bytes] = []

    def send(_target: CaptureTarget, request: bytes) -> tuple[int, bytes]:
        calls.append(request)
        raise AssertionError("unvalidated target reached transport")

    with pytest.raises(ValueError, match="loopback"):
        capture_bar_chart_qa(
            payload,
            targets.model_dump_json().encode().replace(b"127.0.0.1", b"example.com"),
            transport=send,
        )
    assert calls == []


@pytest.mark.parametrize("response", [b'{"schema_version":"chart-qa-v1"}', b"x" * 1_048_577])
def test_unbounded_or_wrong_version_response_is_not_a_v2_capture(
    response: bytes,
) -> None:
    payload = GOLD_PATH.read_bytes()
    targets = targets_fixture(load_bar_gold(payload))

    def send(_target: CaptureTarget, _request: bytes) -> tuple[int, bytes]:
        return 200, response

    with pytest.raises(ValueError):
        capture_bar_chart_qa(payload, targets.model_dump_json().encode(), transport=send)
