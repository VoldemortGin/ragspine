"""The real UI never inherits upstream provider or persistence settings."""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.http.webui_gate import build_webui_environment


def test_preview_environment_is_private_and_does_not_inherit_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "upstream-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated-provider.example")
    monkeypatch.setenv("DATABASE_URL", "postgresql://another-project/database")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other-project-secret")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "download-me")
    environment = build_webui_environment(
        data_dir=tmp_path, secret="local-session-secret", preview=True
    )
    assert "upstream-secret" not in environment.values()
    assert "OPENAI_BASE_URL" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["DATABASE_URL"] == f"sqlite:///{tmp_path}/webui.db"
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8766/v1"
    assert environment["ENTERPRISE_WEBUI_VERSION"] == "0.6.5"
    assert environment["RAG_EMBEDDING_ENGINE"] == "openai"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["RAG_RERANKING_MODEL"] == ""
