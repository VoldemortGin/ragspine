"""Resolve user configuration into one immutable, source-attributed plan."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ragspine.config.models import RAGSpineConfig

PresetName = Literal["economy", "balanced", "quality"]
ConfigSource = Literal["default", "preset", "config", "legacy_profile"]

_RECIPES: dict[PresetName, dict[str, str]] = {
    "economy": {
        "retrieval_mode": "economy",
        "embedding": "none",
        "vector_store": "none",
        "reranker": "none",
        "postprocessor": "none",
    },
    "balanced": {
        "retrieval_mode": "hybrid",
        "embedding": "deterministic",
        "vector_store": "in_process",
        "reranker": "none",
        "postprocessor": "none",
    },
    "quality": {
        "retrieval_mode": "hybrid",
        "embedding": "onnx",
        "vector_store": "in_process",
        "reranker": "cross_encoder",
        "postprocessor": "mmr,lost_in_middle,compress",
    },
}


class SourceEntry(BaseModel):
    """Origin of one effective leaf value."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    source: ConfigSource


class EffectivePlan(BaseModel):
    """Fully resolved configuration plus immutable per-leaf provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    config: RAGSpineConfig
    sources: tuple[SourceEntry, ...]

    @property
    def source_map(self) -> dict[str, ConfigSource]:
        """Return a defensive path-to-source mapping."""
        return {entry.path: entry.source for entry in self.sources}

    @property
    def leaf_values(self) -> dict[str, object]:
        """Return all effective leaf values, keyed by dotted path."""
        return _flatten(self.config.model_dump(mode="python"))

    def source_for(self, path: str) -> ConfigSource:
        """Return the recorded origin for one dotted configuration path."""
        try:
            return self.source_map[path]
        except KeyError as error:
            raise KeyError(f"unknown effective configuration path: {path}") from error


def _flatten(value: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            leaves.update(_flatten(item, path))
        else:
            leaves[path] = item
    return leaves


def _explicit_paths(value: Mapping[str, object], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            paths.update(_explicit_paths(item, path))
        else:
            paths.add(path)
    return paths


def resolve_config(
    *,
    preset: PresetName | None = None,
    profile: PresetName | None = None,
    config: RAGSpineConfig | Mapping[str, object] | None = None,
) -> EffectivePlan:
    """Resolve a preset and explicit overrides into one effective plan.

    ``profile`` is the legacy spelling.  Supplying it together with ``preset`` is
    rejected so call-site intent remains unambiguous.
    """
    if preset is not None and profile is not None:
        raise ValueError("preset and legacy profile cannot be supplied together")

    if config is None:
        payload: dict[str, object] = {}
        explicit_paths: set[str] = set()
    elif isinstance(config, RAGSpineConfig):
        payload = config.model_dump(mode="python", exclude_unset=True)
        explicit_paths = _explicit_paths(payload)
    else:
        payload = dict(config)
        explicit_paths = _explicit_paths(payload)

    selected_input = preset or profile or payload.get("profile", "economy")
    selected = RAGSpineConfig.model_validate({"profile": selected_input}).profile
    payload["profile"] = selected
    unresolved = RAGSpineConfig.model_validate(payload)

    recipe = _RECIPES[selected]
    retrieval = unresolved.retrieval.model_dump(mode="python")
    for field, recipe_value in recipe.items():
        if retrieval[field] is None:
            retrieval[field] = recipe_value
    effective_payload = unresolved.model_dump(mode="python")
    effective_payload["retrieval"] = retrieval
    effective = RAGSpineConfig.model_validate(effective_payload)

    if preset is not None:
        profile_source: ConfigSource = "preset"
    elif profile is not None:
        profile_source = "legacy_profile"
    elif "profile" in explicit_paths:
        profile_source = "config"
    else:
        profile_source = "default"

    sources: list[SourceEntry] = []
    for path in _flatten(effective.model_dump(mode="python")):
        if path in explicit_paths and path != "profile":
            source: ConfigSource = "config"
        elif path == "profile" or path.startswith("retrieval."):
            source = profile_source
        else:
            source = "default"
        sources.append(SourceEntry(path=path, source=source))
    return EffectivePlan(config=effective, sources=tuple(sources))


__all__ = ["ConfigSource", "EffectivePlan", "PresetName", "SourceEntry", "resolve_config"]
