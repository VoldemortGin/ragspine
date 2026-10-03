"""configs reads the project-root .env below the real environment; secrets stay redacted."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import AliasChoices, SecretStr

from ragspine.common.evidence.configs import ROOT_DIR, Settings
from ragspine.common.evidence.providers.local_model_tunnel import load_tunnel_config
from ragspine.common.evidence.providers.providers import (
    ProviderConfigurationError,
    load_llm_config,
    load_local_model_config,
)

_LLM_NAMES = ("APP_LLM_API_KEY", "APP_LLM_BASE_URL", "APP_LLM_MODEL")

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
    with pytest.raises(ProviderConfigurationError, match="OPENAI_EMBEDDING_MODEL"):
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


def _alias_names(field: str) -> set[str]:
    alias = Settings.model_fields[field].validation_alias
    return (
        {str(choice) for choice in alias.choices[1:]} if isinstance(alias, AliasChoices) else set()
    )


def _primary_name(field: str) -> str:
    """The name `.env.example` documents: the first alias, else the APP_-prefixed field name."""
    alias = Settings.model_fields[field].validation_alias
    if isinstance(alias, AliasChoices):
        return str(alias.choices[0])
    return alias if isinstance(alias, str) else "APP_" + field.upper()


def test_env_example_lists_exactly_the_settings_fields() -> None:
    text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    listed = set(
        re.findall(r"^#?\s*((?:APP|NB|OPENAI|DATASET|PDF)_[A-Z_]+)=", text, re.MULTILINE)
    ) - {"APP_ROOT_DIR"}
    primaries = {_primary_name(name) for name in Settings.model_fields}
    aliases = set().union(*(_alias_names(name) for name in Settings.model_fields))
    assert primaries <= listed <= primaries | aliases
    assert aliases <= listed


def test_env_example_documents_the_openai_preferred_names() -> None:
    text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_EMBEDDING_MODEL"):
        assert re.search(rf"^{name}=", text, re.MULTILINE), name
    # A separate loopback embedding service is optional: its three names stay commented out.
    for name in ("APP_EMBEDDING_BASE_URL", "APP_EMBEDDING_MODEL", "APP_EMBEDDING_API_KEY"):
        assert not re.search(rf"^{name}=", text, re.MULTILINE), name
        assert re.search(rf"^#\s*{name}=", text, re.MULTILINE), name


# ---- DATASET_PATH:questions_path 的回落别名(主名 NB_QUESTIONS_PATH 优先) ----------------------


def _questions(root_relative: str) -> Path:
    return (ROOT_DIR / root_relative).resolve()


def test_dataset_path_alone_sets_questions_path_from_environment(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATASET_PATH", "data/ds.json")
    assert Settings().questions_path == _questions("data/ds.json")  # 相对路径与主名同样相对项目根


def test_primary_name_wins_over_dataset_path_in_environment(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATASET_PATH", "data/ds.json")
    monkeypatch.setenv("NB_QUESTIONS_PATH", "data/nb.json")
    assert Settings().questions_path == _questions("data/nb.json")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_primary_name_falls_back_to_dataset_path_in_environment(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("NB_QUESTIONS_PATH", blank)
    monkeypatch.setenv("DATASET_PATH", "data/ds.json")
    assert Settings().questions_path == _questions("data/ds.json")


def test_both_blank_or_unset_means_none_in_environment(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings().questions_path is None
    monkeypatch.setenv("NB_QUESTIONS_PATH", "")
    monkeypatch.setenv("DATASET_PATH", " ")
    assert Settings().questions_path is None


def test_dataset_path_alone_in_dotenv(bare: Callable[[str], None]) -> None:
    bare("DATASET_PATH=data/ds.json\n")
    assert Settings().questions_path == _questions("data/ds.json")


def test_primary_name_wins_over_dataset_path_in_dotenv(bare: Callable[[str], None]) -> None:
    bare("DATASET_PATH=data/ds.json\nNB_QUESTIONS_PATH=data/nb.json\n")
    assert Settings().questions_path == _questions("data/nb.json")
    bare("NB_QUESTIONS_PATH=data/nb.json\nDATASET_PATH=data/ds.json\n")  # 与书写顺序无关
    assert Settings().questions_path == _questions("data/nb.json")


def test_blank_primary_name_falls_back_to_dataset_path_in_dotenv(
    bare: Callable[[str], None],
) -> None:
    bare("NB_QUESTIONS_PATH=\nDATASET_PATH=data/ds.json\n")
    assert Settings().questions_path == _questions("data/ds.json")


def test_both_blank_means_none_in_dotenv(bare: Callable[[str], None]) -> None:
    bare("NB_QUESTIONS_PATH=\nDATASET_PATH=\n")
    assert Settings().questions_path is None


def test_dataset_path_in_environment_and_primary_name_in_dotenv(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 跨来源仍是「来源整体比较」(与 OPENAI_* / APP_LLM_* 同一机制):真实环境变量的别名盖过 .env 的主名。
    bare("NB_QUESTIONS_PATH=data/nb.json\n")
    monkeypatch.setenv("DATASET_PATH", "data/ds.json")
    assert Settings().questions_path == _questions("data/ds.json")


# ---- OPENAI_* preferred names (APP_LLM_* aliases) for the three LLM settings -------------------------------------------

_OPENAI = {
    "OPENAI_API_KEY": "openai-secret",
    "OPENAI_BASE_URL": "https://openai.example/v1",
    "OPENAI_MODEL": "openai-model",
}


_APP_LLM = {
    "APP_LLM_API_KEY": "alias-secret",
    "APP_LLM_BASE_URL": "https://alias.example/v1",
    "APP_LLM_MODEL": "alias-model",
}


@pytest.fixture
def bare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """No .env content and no APP_* in the environment; call it to write a .env."""
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    for name in list(os.environ):
        if name.startswith("APP_") and name != "APP_ROOT_DIR":
            monkeypatch.delenv(name)
    path = tmp_path / ".env"
    monkeypatch.setitem(Settings.model_config, "env_file", path)

    def write(content: str) -> None:
        path.write_text(content, encoding="utf-8")

    return write


def test_openai_environment_variables_alone_configure_the_llm(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    llm = load_llm_config()
    assert (llm.base_url, llm.model) == ("https://openai.example/v1", "openai-model")
    assert llm.api_key.get_secret_value() == "openai-secret"
    assert Settings().as_environment(_LLM_NAMES) == {
        "APP_LLM_API_KEY": "openai-secret",
        "APP_LLM_BASE_URL": "https://openai.example/v1",
        "APP_LLM_MODEL": "openai-model",
    }


def test_app_llm_aliases_alone_configure_the_llm(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _APP_LLM.items():
        monkeypatch.setenv(name, value)
    llm = load_llm_config()
    assert (llm.base_url, llm.model) == ("https://alias.example/v1", "alias-model")
    assert llm.api_key.get_secret_value() == "alias-secret"
    assert Settings().as_environment(_LLM_NAMES) == _APP_LLM


def test_openai_names_win_and_the_choice_is_per_field(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_LLM_MODEL", "app-model")
    monkeypatch.setenv("APP_LLM_API_KEY", "app-secret")
    llm = load_llm_config()
    assert llm.model == "openai-model"
    assert llm.api_key.get_secret_value() == "openai-secret"
    # A field only given under its alias still resolves, next to a preferred-name field.
    monkeypatch.delenv("OPENAI_MODEL")
    assert load_llm_config().model == "app-model"
    assert load_llm_config().base_url == "https://openai.example/v1"


def test_alias_names_in_dotenv_also_work(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("APP_LLM_API_KEY=dotenv-app-secret\nAPP_LLM_BASE_URL=https://dotenv.example\n")
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    llm = load_llm_config()
    assert llm.api_key.get_secret_value() == "dotenv-app-secret"
    assert (llm.base_url, llm.model) == ("https://dotenv.example", "environment-model")
    # Inside one source the OPENAI_ name wins.
    bare("APP_LLM_MODEL=dotenv-app-model\nOPENAI_MODEL=dotenv-openai-model\n")
    monkeypatch.delenv("OPENAI_MODEL")
    assert Settings().llm_model == "dotenv-openai-model"


def test_across_sources_the_real_environment_wins_even_through_an_alias_name(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # pydantic-settings resolves a field source by source: a shell APP_LLM_MODEL therefore
    # outranks the project .env's OPENAI_MODEL, exactly as OPENAI_MODEL in the shell would.
    bare("OPENAI_MODEL=dotenv-openai-model\n")
    monkeypatch.setenv("APP_LLM_MODEL", "environment-app-model")
    assert Settings().llm_model == "environment-app-model"
    # ...and the real environment's OPENAI_ name still beats its own APP_LLM_ name.
    monkeypatch.setenv("OPENAI_MODEL", "environment-openai-model")
    assert Settings().llm_model == "environment-openai-model"


def test_fields_stay_constructible_by_name(bare: Callable[[str], None]) -> None:
    settings = Settings(llm_model="by-name", llm_api_key=SecretStr("by-name-secret"))
    assert settings.llm_model == "by-name"
    assert settings.as_environment(("APP_LLM_MODEL", "APP_LLM_API_KEY")) == {
        "APP_LLM_MODEL": "by-name",
        "APP_LLM_API_KEY": "by-name-secret",
    }


def test_missing_llm_names_both_variables_preferred_first(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for field, app, openai in (
        ("API_KEY", "APP_LLM_API_KEY", "OPENAI_API_KEY"),
        ("BASE_URL", "APP_LLM_BASE_URL", "OPENAI_BASE_URL"),
        ("MODEL", "APP_LLM_MODEL", "OPENAI_MODEL"),
    ):
        environment = {f"APP_LLM_{name}": "https://x.example" for name in ("API_KEY", "BASE_URL")}
        environment["APP_LLM_MODEL"] = "m"
        del environment[f"APP_LLM_{field}"]
        with pytest.raises(ProviderConfigurationError, match=rf"{openai} \(or {app}\)"):
            load_llm_config(environment)
    with pytest.raises(ProviderConfigurationError, match=r"OPENAI_API_KEY \(or APP_LLM_API_KEY\)"):
        load_llm_config()


def test_injected_mapping_has_no_openai_fallback(bare: Callable[[str], None]) -> None:
    with pytest.raises(ProviderConfigurationError, match="APP_LLM_API_KEY"):
        load_llm_config(dict(_OPENAI))


def test_openai_base_url_is_still_https_only_and_credential_free(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")
    for rejected in (
        "http://openai.example/v1",
        "https://user:pw@openai.example/v1",
        "https://openai.example/v1?key=1",
    ):
        monkeypatch.setenv("OPENAI_BASE_URL", rejected)
        with pytest.raises(ProviderConfigurationError, match="Invalid service base URL"):
            load_llm_config()


def test_openai_secret_never_appears_in_repr(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    settings = Settings()
    rendered = repr(settings) + str(settings) + settings.model_dump_json() + repr(load_llm_config())
    assert "openai-secret" not in rendered


def test_openai_names_alone_never_configure_embedding_or_rerank(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.embedding_api_key is None
    assert settings.embedding_base_url is None
    assert settings.rerank_model is None
    assert settings.as_environment(_EMBEDDING_NAMES) == {}
    with pytest.raises(ProviderConfigurationError, match="OPENAI_EMBEDDING_MODEL"):
        load_local_model_config("embedding")
    with pytest.raises(ProviderConfigurationError, match="APP_RERANK_BASE_URL"):
        load_local_model_config("rerank")


# ---- embedding on the shared LLM gateway (OPENAI_EMBEDDING_MODEL) ---------------------------

_EMBEDDING_NAMES = ("APP_EMBEDDING_BASE_URL", "APP_EMBEDDING_MODEL", "APP_EMBEDDING_API_KEY")


def test_openai_embedding_model_alone_puts_embedding_on_the_llm_gateway(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("OPENAI_EMBEDDING_MODEL=gateway-embedding\n")
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    embedding = load_local_model_config("embedding")
    assert (embedding.base_url, embedding.model) == (
        "https://openai.example/v1",
        "gateway-embedding",
    )
    assert embedding.api_key.get_secret_value() == "openai-secret"
    # Children get the resolved group under the APP_EMBEDDING_* allowlist names.
    assert Settings().as_environment(_EMBEDDING_NAMES) == {
        "APP_EMBEDDING_BASE_URL": "https://openai.example/v1",
        "APP_EMBEDDING_MODEL": "gateway-embedding",
        "APP_EMBEDDING_API_KEY": "openai-secret",
    }
    # ...and a child given exactly that (plus the LLM base) resolves the same configuration.
    child = Settings().as_environment((*_EMBEDDING_NAMES, "APP_LLM_BASE_URL"))
    assert load_local_model_config("embedding", child) == embedding


def test_an_embedding_key_of_its_own_overrides_the_llm_key_on_the_gateway(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("OPENAI_EMBEDDING_MODEL=gateway-embedding\nAPP_EMBEDDING_API_KEY=embedding-only\n")
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    assert load_local_model_config("embedding").api_key.get_secret_value() == "embedding-only"
    assert Settings().as_environment(("APP_EMBEDDING_API_KEY",)) == {
        "APP_EMBEDDING_API_KEY": "embedding-only"
    }


def test_a_blank_embedding_base_url_from_an_old_dotenv_still_means_the_gateway(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("APP_EMBEDDING_BASE_URL=\nAPP_EMBEDDING_MODEL=app-named-embedding\n")
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    embedding = load_local_model_config("embedding")
    assert (embedding.base_url, embedding.model) == (
        "https://openai.example/v1",
        "app-named-embedding",
    )


def test_a_separate_embedding_base_url_keeps_the_loopback_rules(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _OPENAI.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "gateway-embedding")
    monkeypatch.setenv("APP_EMBEDDING_BASE_URL", "http://127.0.0.1:39002")
    # No fallback to the LLM key for a separate service.
    with pytest.raises(ProviderConfigurationError, match="APP_EMBEDDING_API_KEY"):
        load_local_model_config("embedding")
    assert Settings().as_environment(("APP_EMBEDDING_API_KEY",)) == {}
    monkeypatch.setenv("APP_EMBEDDING_API_KEY", "loopback-secret")
    embedding = load_local_model_config("embedding")
    assert embedding.base_url == "http://127.0.0.1:39002"
    assert embedding.api_key.get_secret_value() == "loopback-secret"
    monkeypatch.setenv("APP_EMBEDDING_BASE_URL", "https://elsewhere.example/v1")
    with pytest.raises(ProviderConfigurationError, match="loopback"):
        load_local_model_config("embedding")


def test_the_embedding_model_prefers_the_openai_name(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("APP_EMBEDDING_MODEL=app-named\nOPENAI_EMBEDDING_MODEL=openai-named\n")
    assert Settings().embedding_model == "openai-named"
    assert Settings(embedding_model="by-name").embedding_model == "by-name"


def test_the_tunnel_launcher_settings_carry_the_openai_embedding_model(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragspine.common.evidence.providers.local_model_launcher import (
        build_local_model_child_environment,
    )
    from ragspine.common.evidence.providers.local_model_tunnel import LocalModelTunnelConfig

    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "gateway-embedding")
    parent = Settings().as_environment(("APP_EMBEDDING_MODEL", "APP_RERANK_MODEL"))
    tunnel = LocalModelTunnelConfig(
        ssh_host="operator@gpu.example",
        ssh_port=22,
        embedding_local_port=39002,
        embedding_remote_port=28002,
        rerank_local_port=39001,
        rerank_remote_port=28001,
    )
    child = build_local_model_child_environment(
        parent | {"APP_RERANK_MODEL": "rerank-model"},
        tunnel,
        embedding_api_key=SecretStr("e"),
        rerank_api_key=SecretStr("r"),
    )
    assert child["APP_EMBEDDING_MODEL"] == "gateway-embedding"


# ---- NB_* notebook settings ---------------------------------------------------------------


def test_notebook_settings_default_to_none(bare: Callable[[str], None]) -> None:
    settings = Settings()
    assert settings.pdf_source_dir is None
    assert settings.questions_path is None
    assert settings.report_dir is None


def test_notebook_settings_resolve_home_and_root_relative_paths(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("NB_PDF_DIR", "~/pdfs")
    monkeypatch.setenv("NB_QUESTIONS_PATH", "data/questions.json")
    monkeypatch.setenv("NB_REPORT_DIR", str(tmp_path / "absent" / "report"))  # not checked
    settings = Settings()
    assert settings.pdf_source_dir == (tmp_path / "pdfs").resolve()
    assert settings.questions_path == (ROOT_DIR / "data" / "questions.json").resolve()
    assert settings.report_dir == (tmp_path / "absent" / "report").resolve()


def test_notebook_settings_come_from_dotenv_and_blank_means_unset(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("NB_PDF_DIR=pdfs\nNB_REPORT_DIR=\n")
    settings = Settings()
    assert settings.pdf_source_dir == (ROOT_DIR / "pdfs").resolve()
    assert settings.report_dir is None
    monkeypatch.setenv("NB_PDF_DIR", "other")
    assert Settings().pdf_source_dir == (ROOT_DIR / "other").resolve()
    assert Settings(pdf_source_dir=Path("by-name")).pdf_source_dir == (ROOT_DIR / "by-name")


# ---- PDF_INGEST_PASSWORD -----------------------------------------------------------------


def test_pdf_ingest_password_is_unset_by_default_and_blank_means_unset(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings().pdf_ingest_password is None
    bare("PDF_INGEST_PASSWORD=   \n")
    assert Settings().pdf_ingest_password is None
    monkeypatch.setenv("PDF_INGEST_PASSWORD", "")
    assert Settings().pdf_ingest_password is None


def test_pdf_ingest_password_comes_from_dotenv_without_prefix_and_stays_secret(
    bare: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    bare("PDF_INGEST_PASSWORD=dotenv-pdf-secret\nAPP_PDF_INGEST_PASSWORD=prefixed-is-ignored\n")
    settings = Settings()
    assert isinstance(settings.pdf_ingest_password, SecretStr)
    assert settings.pdf_ingest_password.get_secret_value() == "dotenv-pdf-secret"
    rendered = repr(settings) + str(settings) + settings.model_dump_json()
    assert "dotenv-pdf-secret" not in rendered
    monkeypatch.setenv("PDF_INGEST_PASSWORD", "environment-pdf-secret")
    password = Settings().pdf_ingest_password
    assert password is not None and password.get_secret_value() == "environment-pdf-secret"
