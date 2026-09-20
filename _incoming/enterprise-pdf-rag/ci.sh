#!/usr/bin/env bash
# Single read-only completion gate. Install dependencies before invoking it.
set -euo pipefail
test -f .project-root || { echo "Run ./ci.sh from the repository root." >&2; exit 1; }
export UV_OFFLINE=true
uv run --locked ruff format --check .
uv run --locked ruff check --no-fix .
uv run --locked mypy .
uv run --locked python scripts/check_conformance.py .
uv run --locked python scripts/check_architecture.py
uv run --locked python scripts/check_schema.py
uv run --locked python scripts/check_drift.py
uv run --locked pytest
echo "All checks passed."
