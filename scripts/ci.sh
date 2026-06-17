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

echo "==> [1/5] docstring reference integrity (no dead src/ or docs/ links; package indexes match)"
"$PY" scripts/check_docstring_refs.py

echo "==> [2/5] doc-drift (contracts re-verified against their covered code)"
"$PY" scripts/check_doc_drift.py --quiet

echo "==> [3/5] test suite (excludes gpu + docling — the bulk, no heavy 3rd-party models)"
"$PY" -m pytest tests/ -q -m "not gpu and not docling"

echo "==> [4/5] docling extractor tests (own process — isolates 3rd-party ML nondeterminism)"
"$PY" -m pytest tests/ -q -m "docling"

echo "==> [5/5] end-to-end demo smoke"
"$PY" scripts/run_demo.py | tail -1

echo
echo "✅ local CI passed"
