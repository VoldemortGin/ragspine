"""Strict, serializable configuration models for the high-level facade."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SAFE_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RetrievalConfig(_StrictModel):
    """Optional overrides layered over the selected retrieval profile."""

    retrieval_mode: Literal["economy", "hybrid"] | None = None
    embedding: Literal["none", "deterministic", "onnx"] | None = None
    vector_store: Literal["none", "in_process"] | None = None
    reranker: Literal["none", "cross_encoder"] | None = None
    postprocessor: Literal["none", "mmr,lost_in_middle,compress"] | None = None


class IndexingConfig(_StrictModel):
    """Persisted narrative chunking contract for a workspace index."""

    chunker: Literal[
        "none",
        "default",
        "layout",
        "parent_child",
        "sentence_window",
        "semantic",
        "laws",
        "qa",
        "book",
    ] = "none"
    max_chars: int = Field(default=480, ge=1)
    overlap_chars: int = Field(default=80, ge=0)

    @model_validator(mode="after")
    def _overlap_is_smaller_than_chunk(self) -> "IndexingConfig":
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        return self


class GraphConfig(_StrictModel):
    """Workspace GraphRAG selection and bounded query budget."""

    mode: Literal["off", "auto"] = "off"
    max_communities: int = Field(default=20, ge=1, le=100)


class GenerationConfig(_StrictModel):
    """Serializable provider selection; an injected provider object still wins."""

    provider_type: Literal["mock", "anthropic"] = "mock"
    model: str = Field(default="claude-opus-4-8", min_length=1)
    base_url: str | None = None
    reference_date: date | None = None


class SecurityConfig(_StrictModel):
    """Deployment-specific security boundaries; core safety gates are not configurable."""

    allowed_upload_root: str | None = None


class StorageConfig(_StrictModel):
    """Safe workspace-local database filenames."""

    knowledge_db: str = "knowledge.db"
    mapping_db: str = "mapping.db"
    review_db: str = "review.db"
    graph_db: str = "graph.db"

    @field_validator("knowledge_db", "mapping_db", "review_db", "graph_db")
    @classmethod
    def _safe_basename(cls, value: str) -> str:
        stem = value.split(".", 1)[0].upper()
        if (
            value in {".", ".."}
            or value.endswith((".", " "))
            or stem in _WINDOWS_RESERVED
            or _SAFE_DB_NAME.fullmatch(value) is None
        ):
            raise ValueError("database filename must be a workspace-local basename")
        return value


class RAGSpineConfig(_StrictModel):
    """One validated configuration boundary for :meth:`RAGSpine.local`."""

    profile: Literal["economy", "balanced", "quality"] = "economy"
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @model_validator(mode="after")
    def _coherent_retrieval(self) -> "RAGSpineConfig":
        profile_mode = "economy" if self.profile == "economy" else "hybrid"
        profile_embedding = {
            "economy": "none",
            "balanced": "deterministic",
            "quality": "onnx",
        }[self.profile]
        mode = self.retrieval.retrieval_mode or profile_mode
        embedding = self.retrieval.embedding or profile_embedding
        if mode == "economy" and embedding != "none":
            raise ValueError("economy retrieval requires embedding='none'")
        if embedding == "none" and self.retrieval.vector_store not in {None, "none"}:
            raise ValueError("embedding='none' requires vector_store='none'")
        return self


__all__ = [
    "GenerationConfig",
    "GraphConfig",
    "IndexingConfig",
    "RAGSpineConfig",
    "RetrievalConfig",
    "SecurityConfig",
    "StorageConfig",
]
