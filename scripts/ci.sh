#!/usr/bin/env bash
#
# Local CI gate — runs the same checks a CI server would, on your machine.
# No GitHub Actions minutes consumed. This is the single source of truth for "is it green":
# the .githooks/pre-push hook runs it before every push, and .github/workflows/ci.yml
# (dormant / manual-trigger only) runs this very script when you choose to enable server CI.
#
# Usage:
#   scripts/ci.sh                 # use ./.venv if present, else system `python`
#   PYTHON=python3.12 scripts/ci.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=".venv/bin/python"; else PY="python"; fi
fi

echo "==> using interpreter: $("$PY" -c 'import sys; print(sys.executable)')"

echo "==> [1/9] docstring reference integrity (no dead src/ or docs/ links; package indexes match)"
"$PY" scripts/check_docstring_refs.py

echo "==> [2/9] doc-drift (contracts re-verified against their covered code)"
"$PY" scripts/check_doc_drift.py --quiet

echo "==> [3/9] mypy --strict (static type contract — zero-warning gate, half 1 of 2)"
"$PY" -m mypy

echo "==> [4/9] ruff lint + format (whole repo; enterprise_pdf_rag strict set scoped via per-file-ignores, ragspine keeps its E/F/I/W/UP/B)"
"$PY" -m ruff check .
"$PY" -m ruff format --check .

echo "==> [5/9] test suite (excludes gpu + docling + network — the bulk; filterwarnings=error + beartype runtime contracts active; includes tests/enterprise_pdf_rag, whose own conftest enforces no-network)"
# The 1,000-file catalog exporter is intentionally isolated: after PDF/OCR native
# libraries have raised the main process footprint, APFS copies become pathologically
# slow. A fresh process keeps the same contract deterministic and cuts minutes from CI.
"$PY" -m pytest tests/workflows/test_workflow_catalog_export.py -q
"$PY" -m pytest tests/ -q -m "not gpu and not docling and not network" \
  --ignore=tests/workflows/test_workflow_catalog_export.py

echo "==> [6/9] docling extractor tests (own process — isolates 3rd-party ML nondeterminism)"
# `[pdf-docling]` is an optional extra; on a lean gate (no docling installed) every docling
# test self-skips and pytest exits 5 ("no tests ran"). Tolerate ONLY that — any real failure
# (exit 1) still propagates and fails the gate.
"$PY" -m pytest tests/ -q -m "docling" || { rc=$?; [ "$rc" -eq 5 ] \
  && echo "  (no docling tests ran — [pdf-docling] not installed; lane skipped)" \
  || exit "$rc"; }

echo "==> [7/9] QA eval + baseline ratchet (4-gate: numeric / citation / refusal / clarification + fabrication; W5 groundedness: faithfulness / answer-accuracy; ratchets up, never down)"
# tool = zero-LLM deterministic direct test; agent = answer_question + MockProvider.
# W5 groundedness uses the offline deterministic default (lexical-overlap entailment) — no model
# download, no network, runs in CI; the opt-in ONNX-NLI / LLM-judge adapters are follow-ups.
# Each mode exits 1 on any gate regression or fabrication increase vs data/golden/qa_baseline.json.
# Both modes already have a committed baseline → pure compare, no baseline file is written here.
"$PY" scripts/run_qa_eval.py --mode tool
"$PY" scripts/run_qa_eval.py --mode agent

echo "==> [8/9] end-to-end demo smoke"
"$PY" scripts/run_demo.py | tail -1

echo "==> [9/9] enterprise_pdf_rag structural gates (scoped to src/enterprise_pdf_rag + docs/enterprise-pdf-rag; never judge ragspine by them)"
# conformance: src layout / beartype claw hook / core-settings leaf / absolute imports / closed
#   import whitelist outside adapters/.  architecture: pure figures/documents/processing (no IO).
#   schema: versioned public JSON contracts under docs/enterprise-pdf-rag/schemas/ vs pydantic models.
#   drift: `covers:` paths in enterprise_pdf_rag docs still exist.
# Their pytest suite already ran inside step 5 (tests/enterprise_pdf_rag/, same collection).
"$PY" scripts/enterprise_pdf_rag/check_conformance.py .
"$PY" scripts/enterprise_pdf_rag/check_architecture.py
"$PY" scripts/enterprise_pdf_rag/check_schema.py
"$PY" scripts/enterprise_pdf_rag/check_drift.py

echo
echo "✅ local CI passed"
