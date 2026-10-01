"""One gold case over an injected chat transport: the body sent and the verdict judged."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from enterprise_pdf_rag.adapters.nl_gold import NlGoldCase
from enterprise_pdf_rag.adapters.nl_gold_runner import ENVELOPE_KEY, build_body, run_case

ROOT = Path(__file__).resolve().parents[3]
_SHA = "ab" * 32
_CASE = NlGoldCase.model_validate_json(
    json.dumps(
        {
            "case_id": "page-two",
            "case_class": "positive",
            "question": {"en": "What does page 2 say?"},
            "document_sha256": _SHA,
            "request": {"rerank": True, "filters": {"periods": ["1H26"], "regions": []}},
            "expected": {
                "status": "answered",
                "min_claims": 1,
                "required_claims": [
                    {"kind": "quote", "page_index": 1, "field_path_prefix": "fragments."}
                ],
            },
            "rationale": "The page-2 line is quoted verbatim.",
        }
    )
)


def _envelope(*, page_index: int) -> dict[str, Any]:
    return {
        "status": "answered",
        "abstain_reason": None,
        "claims": [
            {
                "kind": "quote",
                "text": "Revenue rose",
                "citations": [
                    {
                        "page_index": page_index,
                        "field_path": "fragments.s1",
                        "quote": "Revenue rose",
                    }
                ],
            }
        ],
    }


def _response(envelope: dict[str, Any]) -> dict[str, Any]:
    message = "It reads: Revenue rose\n\n引用:\n[1] p.2 fragments.s1"
    return {"choices": [{"message": {"content": message}}], ENVELOPE_KEY: envelope}


def test_the_body_names_the_document_and_carries_rerank_and_filters() -> None:
    assert build_body(_CASE) == {
        "model": "enterprise-pdf-rag/" + _SHA[:12],
        "messages": [{"role": "user", "content": "What does page 2 say?"}],
        "rerank": True,
        "filters": {"periods": ["1H26"], "regions": []},
    }


def test_a_matching_envelope_passes_and_the_post_receives_the_body() -> None:
    sent: list[dict[str, Any]] = []

    def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sent.append(body)
        return 200, _response(_envelope(page_index=1))

    outcome = run_case(_CASE, post)

    assert sent == [build_body(_CASE)]
    assert (outcome.verdict, outcome.failures, outcome.status_code) == ("pass", (), 200)
    assert outcome.as_json()["observed"]["claim_count"] == 1


def test_a_wrong_page_fails_and_an_http_error_is_reported() -> None:
    wrong = run_case(_CASE, lambda _body: (200, _response(_envelope(page_index=4))))
    assert wrong.verdict == "FAIL"
    assert wrong.failures == ("missing required claim: quote p1 fragments.*",)

    refused = run_case(_CASE, lambda _body: (503, {"detail": "no model"}))
    assert refused.verdict == "FAIL" and refused.status_code == 503
    assert refused.failures[0].startswith("HTTP 503:")


def test_the_live_script_keeps_its_command_line() -> None:
    script = ROOT / "scripts" / "enterprise_pdf_rag" / "nl_gold_eval.py"
    shown = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout
    assert shown.startswith("usage: nl_gold_eval.py [-h] [--gold GOLD] [--base-url BASE_URL]")
    for option in ("--gold", "--base-url", "--out", "--timeout", "--case"):
        assert option in shown
