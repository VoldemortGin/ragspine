"""Strict high-level configuration.

Submodules:
    models.py — frozen Pydantic models for indexing, retrieval, graph, generation, security, and storage.
    resolution.py — deterministic preset resolution and effective-plan provenance.
    workspace.py — persisted index-fingerprint compatibility guard.
"""

from ragspine.config.models import (
    GenerationConfig,
    GraphConfig,
    IndexingConfig,
    RAGSpineConfig,
    RetrievalConfig,
    SecurityConfig,
    StorageConfig,
)
from ragspine.config.resolution import EffectivePlan, SourceEntry, resolve_config
from ragspine.config.workspace import ReindexRequiredError

__all__ = [
    "GenerationConfig",
    "GraphConfig",
    "IndexingConfig",
    "EffectivePlan",
    "RAGSpineConfig",
    "ReindexRequiredError",
    "RetrievalConfig",
    "SecurityConfig",
    "StorageConfig",
    "SourceEntry",
    "resolve_config",
]
