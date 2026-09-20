FROM ghcr.io/astral-sh/uv:0.6.2 AS uv
FROM python:3.12.11-slim-bookworm

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md .python-version .project-root ./
COPY configs/ ./configs/
COPY src/ ./src/
RUN uv sync --locked --no-dev --extra pdf --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    APP_ROOT_DIR=/app \
    APP_EXECUTION_MODE=aia-source-review \
    PYTHON_DOTENV_DISABLED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
CMD ["python", "-m", "uvicorn", "enterprise_pdf_rag.adapters.http.app:create_configured_app", "--factory", "--host", "0.0.0.0", "--port", "8766"]
