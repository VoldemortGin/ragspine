#!/usr/bin/env bash
# Thin entry point; webui_preview.py remains the sole process manager.
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$PROJECT_ROOT"

if [[ $# -ne 0 ]]; then
    echo "Usage: $0" >&2
    exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is missing. Install uv, then run: uv sync --locked --extra pdf" >&2
    exit 1
fi

PROJECT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PROJECT_PYTHON" ]]; then
    echo "Project Python 3.12 environment is missing. In $PROJECT_ROOT run: uv sync --locked --extra pdf" >&2
    exit 1
fi
export APP_ROOT_DIR="$PROJECT_ROOT"
if ! "$PROJECT_PYTHON" -B -I -c 'import sys; sys.exit(1) if sys.version_info[:2] != (3, 12) else None; import pydantic, uvicorn, enterprise_pdf_rag' >/dev/null 2>&1; then
    echo "Project Python 3.12 dependencies are incomplete. Run: uv sync --locked --extra pdf" >&2
    exit 1
fi

exec uv run --locked --offline --no-sync --no-env-file --no-python-downloads \
    --python "$PROJECT_PYTHON" "$PROJECT_PYTHON" \
    "$PROJECT_ROOT/scripts/webui_preview.py" start --require-processing
