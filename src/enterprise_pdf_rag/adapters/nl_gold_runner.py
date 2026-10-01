"""Run one natural-language gold case over any chat transport and judge its response.

``adapters/nl_gold`` owns the schema and the one pass/fail rule and performs no I/O; this
module is the thin runner half both live runners share: build the
``POST /v1/chat/completions`` body for a case, send it through an injected ``post``
(urllib against a running service in ``scripts/enterprise_pdf_rag/nl_gold_eval.py``, an
in-process ASGI client in ``adapters/folder_pipeline``) and judge the envelope.
"""

import json
from collections.abc import Callable
from time import perf_counter
from typing import Any

from enterprise_pdf_rag.adapters.nl_gold import NlGoldCase, ObservedAnswer, answer_prose, judge

ENVELOPE_KEY = "enterprise_pdf_rag"

# One chat request: the JSON body in, the HTTP status and the decoded JSON response out.
type ChatPost = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


class CaseOutcome:
    """One case's live response and verdict."""

    def __init__(
        self,
        case: NlGoldCase,
        *,
        status_code: int,
        elapsed_ms: float,
        response: dict[str, Any] | None,
        failures: tuple[str, ...],
    ) -> None:
        self.case = case
        self.status_code = status_code
        self.elapsed_ms = elapsed_ms
        self.response = response
        self.failures = failures

    @property
    def known_gap(self) -> bool:
        return self.case.expected.known_gap

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def verdict(self) -> str:
        if self.passed:
            return "known-gap-holds" if self.known_gap else "pass"
        return "known-gap-moved" if self.known_gap else "FAIL"

    def as_json(self) -> dict[str, Any]:
        envelope = (self.response or {}).get(ENVELOPE_KEY, {})
        return {
            "case_id": self.case.case_id,
            "case_class": self.case.case_class,
            "question": self.case.question.text,
            "verdict": self.verdict,
            "known_gap": self.known_gap,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "failures": list(self.failures),
            "observed": {
                "status": envelope.get("status"),
                "abstain_reason": envelope.get("abstain_reason"),
                "abstain_detail": envelope.get("abstain_detail"),
                "claim_count": len(envelope.get("claims", ())),
                "filters_applied": envelope.get("filters_applied"),
                "filters_relaxed": envelope.get("filters_relaxed"),
                "cache_hit": envelope.get("cache_hit"),
                "llm_live_calls": envelope.get("llm_live_calls"),
                "snapshot_id": envelope.get("snapshot_id"),
            },
        }


def build_body(case: NlGoldCase) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "enterprise-pdf-rag/" + case.document_sha256[:12],
        "messages": [{"role": "user", "content": case.question.text}],
    }
    if case.request.rerank:
        body["rerank"] = True
    if case.request.filters is not None:
        body["filters"] = {
            "periods": list(case.request.filters.periods),
            "regions": list(case.request.filters.regions),
        }
    return body


def run_case(case: NlGoldCase, post: ChatPost) -> CaseOutcome:
    started = perf_counter()
    status_code, response = post(build_body(case))
    elapsed_ms = (perf_counter() - started) * 1000
    if status_code != 200 or ENVELOPE_KEY not in response:
        detail = json.dumps(response, ensure_ascii=False)[:200]
        return CaseOutcome(
            case,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            response=response,
            failures=(f"HTTP {status_code}: {detail}",),
        )
    observed = ObservedAnswer.model_validate(response[ENVELOPE_KEY])
    message = response["choices"][0]["message"]["content"]
    failures = judge(case, observed, answer_prose(message))
    return CaseOutcome(
        case,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        response=response,
        failures=failures,
    )
