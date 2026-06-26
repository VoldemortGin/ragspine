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

echo "==> [1/8] docstring reference integrity (no dead src/ or docs/ links; package indexes match)"
"$PY" scripts/check_docstring_refs.py

echo "==> [2/8] doc-drift (contracts re-verified against their covered code)"
"$PY" scripts/check_doc_drift.py --quiet

echo "==> [3/8] mypy --strict (static type contract — zero-warning gate, half 1 of 2)"
"$PY" -m mypy

echo "==> [4/8] ruff lint (style + import order + dead code)"
"$PY" -m ruff check src/ragspine

echo "==> [5/8] test suite (excludes gpu + docling + network — the bulk; filterwarnings=error + beartype runtime contracts active)"
"$PY" -m pytest tests/ -q -m "not gpu and not docling and not network"

echo "==> [6/8] docling extractor tests (own process — isolates 3rd-party ML nondeterminism)"
"$PY" -m pytest tests/ -q -m "docling"

echo "==> [7/8] QA 4-gate eval + baseline ratchet (numeric / citation / refusal / clarification + fabrication; ratchets up, never down)"
# tool = zero-LLM deterministic direct test; agent = answer_question + MockProvider.
# Each mode exits 1 on any gate regression or fabrication increase vs data/golden/qa_baseline.json.
# Both modes already have a committed baseline → pure compare, no baseline file is written here.
"$PY" scripts/run_qa_eval.py --mode tool
"$PY" scripts/run_qa_eval.py --mode agent

echo "==> [8/8] end-to-end demo smoke"
"$PY" scripts/run_demo.py | tail -1

echo
echo "✅ local CI passed"
