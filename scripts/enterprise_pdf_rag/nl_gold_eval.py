"""Run the frozen natural-language gold set against a live ``document-catalog`` service.

This is the half the offline replay cannot cover: real retrieval over the published index,
a real answer model, and the completion cache. It sends one
``POST /v1/chat/completions`` per case, judges every response with the same
``adapters.nl_gold.judge`` the offline runner uses, and writes a Markdown table plus the
raw responses so a release can point at evidence instead of a recollection.

Usage (from the repository root, with the service already running):

    .venv/bin/python scripts/enterprise_pdf_rag/nl_gold_eval.py
    .venv/bin/python scripts/enterprise_pdf_rag/nl_gold_eval.py \
        --base-url http://127.0.0.1:8768 --out data/validation/nl-gold/2026-09-20

Exit code 1 as soon as one case that is **not** a known gap fails; a known gap is reported
in its own section and never fails the run.
"""

import argparse
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from enterprise_pdf_rag.adapters.nl_gold import NlGoldCase, NlGoldSet, load_gold
from enterprise_pdf_rag.adapters.nl_gold_runner import CaseOutcome, build_body, run_case

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_GOLD = (
    ROOT
    / "data"
    / "benchmarks"
    / "enterprise-pdf-rag"
    / "aia-2026-interim"
    / "nl-answers-gold-v1.json"
)
DEFAULT_BASE_URL = "http://127.0.0.1:8768"


def post(base_url: str, body: dict[str, Any], *, timeout: float) -> tuple[int, dict[str, Any]]:
    # The URL is the operator-supplied loopback service; no redirect or scheme guessing.
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            return int(error.code), json.loads(payload)
        except json.JSONDecodeError:
            return int(error.code), {"detail": payload.decode("utf-8", "replace")[:500]}


def runnable(gold: NlGoldSet) -> tuple[NlGoldCase, ...]:
    """Every case a live service can answer; the adversarial ones script illegal output."""
    return tuple(case for case in gold.cases if not case.offline_only)


def markdown(gold: NlGoldSet, outcomes: Sequence[CaseOutcome], base_url: str) -> str:
    failed = [item for item in outcomes if not item.passed and not item.known_gap]
    gaps = [item for item in outcomes if item.known_gap]
    passed = [item for item in outcomes if item.passed and not item.known_gap]
    lines = [
        "# Natural-language gold set, real-model run",
        "",
        f"- date: {date.today().isoformat()}",
        f"- service: `{base_url}` (`document-catalog` mode)",
        f"- gold: `{gold.schema_version}`, {len(gold.cases)} cases, {len(outcomes)} run here "
        f"({len(gold.cases) - len(outcomes)} are offline-only)",
        f"- pinned release: processing `{gold.pinned.processing_id[:12]}`, "
        f"snapshot `{gold.pinned.snapshot_id[:12]}`, {gold.pinned.member_count} members",
        f"- result: **{len(passed)} passed / {len(failed)} failed / {len(gaps)} known gaps**",
        "",
        "| case | class | verdict | status | claims | filters_applied | relaxed | cache | ms | detail |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in outcomes:
        observed = outcome.as_json()["observed"]
        filters = observed["filters_applied"]
        printed = "-" if filters is None else json.dumps(filters, ensure_ascii=False)
        detail = "; ".join(outcome.failures).replace("|", "\\|") or "-"
        status = observed["status"] or f"HTTP {outcome.status_code}"
        if observed["abstain_reason"]:
            status = f"{status} / {observed['abstain_reason']}"
        lines.append(
            f"| `{outcome.case.case_id}` | {outcome.case.case_class} | {outcome.verdict} | "
            f"{status} | {observed['claim_count']} | {printed} | "
            f"{observed['filters_relaxed']} | {observed['cache_hit']} | "
            f"{outcome.elapsed_ms:.0f} | {detail[:180]} |"
        )
    if gaps:
        lines += ["", "## Known gaps (reported, never failed)", ""]
        for outcome in gaps:
            lines.append(f"- `{outcome.case.case_id}` - {outcome.case.expected.known_gap_detail}")
            if outcome.failures:
                lines.append(f"  - moved from the frozen behaviour: {'; '.join(outcome.failures)}")
    if failed:
        lines += ["", "## Failures", ""]
        for outcome in failed:
            lines.append(f"- `{outcome.case.case_id}` - {'; '.join(outcome.failures)}")
            lines.append(f"  - why the case exists: {outcome.case.rationale}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--case", action="append", default=None, help="run only these case ids")
    arguments = parser.parse_args(argv)

    gold = load_gold(arguments.gold.read_bytes())
    cases = runnable(gold)
    if arguments.case:
        wanted = set(arguments.case)
        cases = tuple(case for case in cases if case.case_id in wanted)
        if not cases:
            print("No gold case matched --case")
            return 2
    out = arguments.out or ROOT / "data" / "validation" / "nl-gold" / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)

    outcomes: list[CaseOutcome] = []
    for case in cases:
        outcome = run_case(
            case, lambda body: post(arguments.base_url, body, timeout=arguments.timeout)
        )
        outcomes.append(outcome)
        print(
            f"{outcome.verdict:16} {case.case_id:34} "
            f"{outcome.elapsed_ms:7.0f}ms  {'; '.join(outcome.failures)[:160]}"
        )
        (out / f"{case.case_id}.json").write_text(
            json.dumps(
                {
                    "request": build_body(case),
                    "status_code": outcome.status_code,
                    "elapsed_ms": round(outcome.elapsed_ms, 1),
                    "response_json": outcome.response,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    failed = [item for item in outcomes if not item.passed and not item.known_gap]
    report = {
        "schema_version": "nl-answers-gold-eval-v1",
        "date": date.today().isoformat(),
        "base_url": arguments.base_url,
        "gold": str(arguments.gold.relative_to(ROOT)),
        "gold_schema_version": gold.schema_version,
        "pinned": gold.pinned.model_dump(mode="json"),
        "totals": {
            "run": len(outcomes),
            "passed": sum(1 for item in outcomes if item.passed and not item.known_gap),
            "failed": len(failed),
            "known_gap": sum(1 for item in outcomes if item.known_gap),
            "offline_only_skipped": len(gold.cases) - len(cases),
        },
        "cases": [item.as_json() for item in outcomes],
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(markdown(gold, outcomes, arguments.base_url), encoding="utf-8")
    print(f"\nreport: {out}/report.md")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
