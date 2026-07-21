"""Deterministic runtime configuration files and effective-value inspection."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    """Raised when a diagnostics configuration file is invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Small, dependency-free projection of settings relevant to diagnostics."""

    db_path: str = "data/fact_metric.db"
    chunk_db_path: str | None = None
    mapping_db_path: str | None = None
    queue_db_path: str | None = None
    manifest_db_path: str | None = None
    provider_type: str = "mock"
    model: str = "claude-sonnet-4-20250514"
    base_url: str | None = None
    retrieval_mode: str = "economy"
    embedding: str = "auto"
    vector_store: str = "none"
    reranker: str = "none"
    postprocessor: str = "none"
    allowed_upload_root: str | None = None
    studio_dir: str | None = None
    n8n_store_path: str = "data/n8n_store"


@dataclass(frozen=True)
class EffectiveConfig:
    """Resolved configuration together with provenance for every value."""

    profile: str
    runtime: RuntimeConfig
    sources: Mapping[str, str]
    path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation without environment secrets."""
        return {
            "profile": self.profile,
            "values": asdict(self.runtime),
            "sources": dict(self.sources),
            "path": str(self.path) if self.path is not None else None,
        }


_PROFILES: dict[str, RuntimeConfig] = {
    "offline": RuntimeConfig(embedding="none"),
    "economy": RuntimeConfig(embedding="none"),
    "balanced": RuntimeConfig(
        retrieval_mode="hybrid", embedding="deterministic", vector_store="in_process"
    ),
    "quality": RuntimeConfig(
        retrieval_mode="hybrid",
        embedding="onnx",
        vector_store="in_process",
        reranker="cross_encoder",
        postprocessor="mmr,lost_in_middle,compress",
    ),
    "service": RuntimeConfig(retrieval_mode="auto"),
}
_FIELD_NAMES = {field.name for field in fields(RuntimeConfig)}
_LEGACY_ENV = {
    "RAGSPINE_PROVIDER": "provider_type",
}


def render_config(profile: str = "offline") -> str:
    """Render a minimal, secret-free TOML file for a supported profile."""
    if profile not in _PROFILES:
        raise ConfigError(f"unknown config profile: {profile!r}")
    preset = _PROFILES[profile]
    return (
        f'profile = "{profile}"\n\n'
        "[service]\n"
        'db_path = "data/fact_metric.db"\n'
        'provider_type = "mock"\n'
        f'retrieval_mode = "{preset.retrieval_mode}"\n'
        f'embedding = "{preset.embedding}"\n'
        f'vector_store = "{preset.vector_store}"\n'
        f'reranker = "{preset.reranker}"\n'
        f'postprocessor = "{preset.postprocessor}"\n'
    )


def init_config(
    path: str | os.PathLike[str], *, profile: str = "offline", force: bool = False
) -> Path:
    """Create a minimal config, refusing accidental replacement by default."""
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(f"configuration already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(profile), encoding="utf-8")
    return target


def _coerce(value: object, *, field_name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ConfigError(f"invalid value for {field_name}: {value!r}")


def _make_runtime(values: Mapping[str, str | None]) -> RuntimeConfig:
    def required(name: str) -> str:
        value = values[name]
        if value is None:
            raise ConfigError(f"{name} cannot be null")
        return value

    return RuntimeConfig(
        db_path=required("db_path"),
        chunk_db_path=values["chunk_db_path"],
        mapping_db_path=values["mapping_db_path"],
        queue_db_path=values["queue_db_path"],
        manifest_db_path=values["manifest_db_path"],
        provider_type=required("provider_type"),
        model=required("model"),
        base_url=values["base_url"],
        retrieval_mode=required("retrieval_mode"),
        embedding=required("embedding"),
        vector_store=required("vector_store"),
        reranker=required("reranker"),
        postprocessor=required("postprocessor"),
        allowed_upload_root=values["allowed_upload_root"],
        studio_dir=values["studio_dir"],
        n8n_store_path=required("n8n_store_path"),
    )


def load_effective_config(
    path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> EffectiveConfig:
    """Resolve profile defaults < TOML < environment < explicit overrides."""
    config_path = Path(path) if path is not None else None
    document: dict[str, object] = {}
    if config_path is not None:
        try:
            with config_path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc

    profile_value = document.get("profile", "offline")
    if not isinstance(profile_value, str) or profile_value not in _PROFILES:
        raise ConfigError(f"unknown config profile: {profile_value!r}")
    service_value = document.get("service", {})
    if not isinstance(service_value, dict):
        raise ConfigError("[service] must be a TOML table")
    unknown = set(service_value) - _FIELD_NAMES
    if unknown:
        raise ConfigError(f"unknown service setting(s): {', '.join(sorted(unknown))}")

    runtime = _PROFILES[profile_value]
    values: dict[str, str | None] = {name: getattr(runtime, name) for name in _FIELD_NAMES}
    sources = {name: "profile" for name in _FIELD_NAMES}
    for name, value in service_value.items():
        values[name] = _coerce(value, field_name=name)
        sources[name] = "file"

    environment = os.environ if env is None else env
    env_fields: dict[str, str] = {}
    for name in _FIELD_NAMES:
        key = f"RAGSPINE_{name.upper()}"
        if key in environment:
            env_fields[name] = environment[key]
    for key, name in _LEGACY_ENV.items():
        if key in environment and name not in env_fields:
            env_fields[name] = environment[key]
    for name, value in env_fields.items():
        values[name] = _coerce(value, field_name=name)
        sources[name] = "env"

    explicit = {} if overrides is None else dict(overrides)
    unknown = set(explicit) - _FIELD_NAMES
    if unknown:
        raise ConfigError(f"unknown override(s): {', '.join(sorted(unknown))}")
    for name, value in explicit.items():
        values[name] = _coerce(value, field_name=name)
        sources[name] = "override"
    return EffectiveConfig(profile_value, _make_runtime(values), sources, config_path)
