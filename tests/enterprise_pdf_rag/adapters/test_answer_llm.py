"""make_answer_llm builds the answer client from settings; callers may override two knobs."""

from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.answer_llm import make_answer_llm
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.providers.providers import ProviderConfigurationError


@pytest.fixture(autouse=True)
def _llm_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_LLM_API_KEY", "answer-secret")
    monkeypatch.setenv("APP_LLM_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("APP_LLM_MODEL", "answer-model")
    monkeypatch.setenv("APP_INGESTION_DIR", str(tmp_path / "ingestion"))
    monkeypatch.setenv("APP_ANSWER_MAX_LIVE_CALLS", "7")
    monkeypatch.setenv("APP_ANSWER_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("APP_ANSWER_SEED", "5")
    get_settings.cache_clear()


def test_defaults_come_from_settings(tmp_path: Path) -> None:
    llm = make_answer_llm()
    assert llm._cache == tmp_path / "ingestion" / "model-cache"
    assert llm._initial_budget == 7
    assert llm._timeout == 33
    assert llm._seed == 5
    assert llm.live_call_count == 0


def test_arguments_override_the_cache_and_the_budget(tmp_path: Path) -> None:
    llm = make_answer_llm(cache_dir=tmp_path / "elsewhere", max_live_calls=0)
    assert llm._cache == tmp_path / "elsewhere"
    assert llm._initial_budget == 0
    assert llm._timeout == 33


def test_openai_names_configure_it_when_the_app_names_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("APP_LLM_API_KEY", "APP_LLM_BASE_URL", "APP_LLM_MODEL"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")
    assert make_answer_llm().fingerprint != ""


def test_an_unconfigured_llm_raises_when_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_LLM_API_KEY")
    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        make_answer_llm()
