#!/usr/bin/env bash
#
# Opt-in lint / type-check — INFORMATIONAL, not a gate (the extracted codebase was never
# linted, so this currently reports findings rather than blocking). Adopt incrementally:
#   .venv/bin/ruff check --fix ragspine scripts tests
#   .venv/bin/ruff format ragspine scripts tests
# When the tree is clean, wire this into scripts/ci.sh as a blocking step.
#
# Usage: scripts/lint.sh   (requires `pip install -e ".[dev]"` for ruff + mypy)
#
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"

echo "==> ruff check (lint)"
"$PY" -m ruff check ragspine scripts tests || true
echo
echo "==> ruff format --check (style)"
"$PY" -m ruff format --check ragspine scripts tests || true
echo
echo "==> mypy (types, best-effort)"
"$PY" -m mypy ragspine || true
echo
echo "(informational only — exit status is always 0)"
