"""configs reads the project-root .env below the real environment; secrets stay redacted."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import ROOT_DIR, Settings
from ragspine.common.evidence.providers.local_model_tunnel import load_tunnel_config
from ragspine.common.evidence.providers.providers import (
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)

_DOTENV = """\
APP_LLM_API_KEY=dotenv-llm-secret
APP_LLM_BASE_URL=https://provider.example/v1
APP_LLM_MODEL=dotenv-model
APP_EMBEDDING_BASE_URL=http://127.0.0.1:39002
APP_EMBEDDING_MODEL=embedding-model
APP_EMBEDDING_API_KEY=dotenv-embedding-secret
APP_TUNNEL_SSH_HOST=operator@gpu.example
APP_TUNNEL_SSH_PORT=6000
APP_TUNNEL_EMBEDDING_LOCAL_PORT=39002
APP_TUNNEL_EMBEDDING_REMOTE_PORT=28002
APP_TUNNEL_RERANK_LOCAL_PORT=39001
APP_TUNNEL_RERANK_REMOTE_PORT=28001
"""


@pytest.fixture
def dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Settings at a temporary .env and clear every in-scope variable from the env."""
    path = tmp_path / ".env"
    path.write_text(_DOTENV, encoding="utf-8")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    for name in list(os.environ):
        if name.startswith("APP_") and name != "APP_ROOT_DIR":
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", path)
    return path


def test_dotenv_values_reach_settings_and_the_loaders(dotenv: Path) -> None:
    settings = Settings()
    assert settings.llm_model == "dotenv-model"
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "dotenv-llm-secret"
    llm = load_llm_config()
    assert (llm.base_url, llm.model) == ("https://provider.example/v1", "dotenv-model")
    assert llm.api_key.get_secret_value() == "dotenv-llm-secret"
    embedding = load_local_model_config("embedding")
    assert embedding.api_key.get_secret_value() == "dotenv-embedding-secret"
    assert load_tunnel_config().embedding_base_url == "http://127.0.0.1:39002"


def test_real_environment_overrides_dotenv(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LLM_MODEL", "environment-model")
    assert Settings().llm_model == "environment-model"
    assert load_llm_config().model == "environment-model"


def test_missing_dotenv_and_unconfigured_groups_fail_only_when_used(
    tmp_path: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", tmp_path / "absent" / ".env")
    settings = Settings()
    assert settings.rerank_model is None
    assert settings.as_environment(("APP_RERANK_MODEL", "APP_LLM_API_KEY")) == {}
    with pytest.raises(ProviderConfigurationError, match="APP_LLM_API_KEY"):
        load_llm_config()
    with pytest.raises(ProviderConfigurationError, match="APP_RERANK_BASE_URL"):
        load_local_model_config("rerank")


def test_dotenv_is_ignored_when_python_dotenv_is_disabled(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    assert Settings().llm_model is None


def test_secrets_never_appear_in_repr(dotenv: Path) -> None:
    settings = Settings()
    rendered = repr(settings) + str(settings) + settings.model_dump_json()
    assert "dotenv-llm-secret" not in rendered
    assert "dotenv-embedding-secret" not in rendered
    assert "dotenv-llm-secret" not in repr(load_llm_config())


def test_injected_mapping_bypasses_settings(dotenv: Path) -> None:
    config = load_llm_config(
        {
            "APP_LLM_API_KEY": "injected-secret",
            "APP_LLM_BASE_URL": "https://injected.example",
            "APP_LLM_MODEL": "injected-model",
        }
    )
    assert config.model == "injected-model"
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_BASE_URL"):
        load_local_model_config("embedding", {})


def test_as_environment_rejects_names_that_are_not_fields(dotenv: Path) -> None:
    with pytest.raises(KeyError):
        Settings().as_environment(("OPENAI_API_KEY",))
    with pytest.raises(KeyError):
        Settings().as_environment(("APP_NOT_A_FIELD",))


def test_project_root_dotenv_is_found_from_a_nested_working_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    notebooks = root / "notebooks"
    notebooks.mkdir(parents=True)
    (root / ".project-root").touch()
    (root / ".env").write_text(_DOTENV + "APP_EXECUTION_MODE=document-catalog\n", encoding="utf-8")
    probe = (
        "import json\n"
        "from ragspine.common.evidence.configs import ROOT_DIR, get_settings\n"
        "from ragspine.common.evidence.providers.providers import load_llm_config\n"
        "s = get_settings()\n"
        "print(json.dumps({'root': str(ROOT_DIR), 'mode': s.execution_mode,"
        " 'model': load_llm_config().model, 'repr': repr(s)}))\n"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("APP_") and name != "PYTHON_DOTENV_DISABLED"
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=notebooks,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["root"]) == root.resolve()
    assert payload["mode"] == "document-catalog"
    assert payload["model"] == "dotenv-model"
    assert "dotenv-llm-secret" not in result.stdout + result.stderr


def test_env_example_lists_exactly_the_settings_fields() -> None:
    text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*(APP_[A-Z_]+)=", text, re.MULTILINE)) - {"APP_ROOT_DIR"}
    assert listed == {"APP_" + name.upper() for name in Settings.model_fields}
