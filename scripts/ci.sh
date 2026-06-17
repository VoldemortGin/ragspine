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

echo "==> [1/2] test suite (excluding gpu-marked integration tests)"
"$PY" -m pytest tests/ -q -m "not gpu"

echo "==> [2/2] end-to-end demo smoke"
"$PY" scripts/run_demo.py | tail -1

echo
echo "✅ local CI passed"
