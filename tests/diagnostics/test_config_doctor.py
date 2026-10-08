from pathlib import Path

import pytest

from ragspine.diagnostics.config import ConfigError, init_config, load_effective_config
from ragspine.diagnostics.doctor import run_doctor


def test_init_config_is_minimal_and_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ragspine.toml"
    assert init_config(path) == path
    assert 'profile = "offline"' in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        init_config(path)
    init_config(path, profile="service", force=True)
    assert 'embedding = "auto"' in path.read_text(encoding="utf-8")


def test_effective_config_precedence_and_provenance(tmp_path: Path) -> None:
    path = tmp_path / "ragspine.toml"
    path.write_text('[service]\nprovider_type = "mock"\nmodel = "from-file"\n', encoding="utf-8")
    config = load_effective_config(
        path,
        env={"RAGSPINE_PROVIDER_TYPE": "anthropic", "RAGSPINE_MODEL": "from-env"},
        overrides={"model": "explicit"},
    )
    assert config.runtime.provider_type == "anthropic"
    assert config.runtime.model == "explicit"
    assert config.sources["provider_type"] == "env"
    assert config.sources["model"] == "override"


def test_effective_config_rejects_unknown_setting(tmp_path: Path) -> None:
    path = tmp_path / "ragspine.toml"
    path.write_text("[service]\nmade_up = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="made_up"):
        load_effective_config(path, env={})


def test_doctor_reports_missing_anthropic_setup_without_exposing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ragspine.diagnostics.doctor.importlib.util.find_spec", lambda _name: None)
    config = load_effective_config(
        env={},
        overrides={
            "provider_type": "anthropic",
            "model": "",
            "db_path": str(tmp_path / "facts.db"),
            "n8n_store_path": str(tmp_path),
        },
    )
    report = run_doctor(config, env={})
    codes = {finding.code for finding in report.findings}
    assert {
        "dependency.anthropic.missing",
        "model.anthropic.empty",
        "key.anthropic.missing",
    } <= codes
    assert report.ok is False
    assert "API_KEY" not in str(report.to_dict().get("config"))


def test_doctor_checks_litellm_dependency_not_unknown_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ragspine.diagnostics.doctor.importlib.util.find_spec", lambda _name: None)
    config = load_effective_config(
        env={},
        overrides={
            "provider_type": "litellm",
            "db_path": str(tmp_path / "facts.db"),
            "n8n_store_path": str(tmp_path),
        },
    )
    codes = {finding.code for finding in run_doctor(config, env={}).findings}
    assert "dependency.litellm.missing" in codes
    assert "provider.unknown" not in codes


def test_doctor_checks_openai_provider_env_not_unknown_provider(tmp_path: Path) -> None:
    config = load_effective_config(
        env={},
        overrides={
            "provider_type": "openai",
            "db_path": str(tmp_path / "facts.db"),
            "n8n_store_path": str(tmp_path),
        },
    )
    codes = {f.code for f in run_doctor(config, env={}).findings}
    assert "key.openai_compat.invalid" in codes and "provider.unknown" not in codes
    ok_env = {
        "APP_LLM_API_KEY": "k",
        "APP_LLM_BASE_URL": "https://gw.example.com/v1",
        "APP_LLM_MODEL": "m",
    }
    codes = {f.code for f in run_doctor(config, env=ok_env).findings}
    assert "key.openai_compat.invalid" not in codes and "provider.unknown" not in codes


def test_doctor_reports_missing_database_parent(tmp_path: Path) -> None:
    config = load_effective_config(
        env={},
        overrides={
            "db_path": str(tmp_path / "missing" / "facts.db"),
            "n8n_store_path": str(tmp_path),
        },
    )
    report = run_doctor(config, env={})
    assert "path.db.parent_missing" in {finding.code for finding in report.findings}
