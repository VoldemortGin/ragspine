"""服务层运行时配置与资源/provider 装配。

集中化配置（CLI / HTTP / worker 共用），避免装配逻辑各处复制。配置由环境变量
或显式构造注入；sqlite store、provider、narrative retriever 在每个请求/任务内
自行打开并关闭，不做跨请求全局单例。
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ragspine.agent.agent import NarrativeRetriever
from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    LLMProvider,
    MockProvider,
)
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.retrieval.vector.persistence_policy import make_persistence_policy
from ragspine.retrieval.vector.store import make_vector_store
from ragspine.storage.fact_store import FactStore


@dataclass(frozen=True)
class ServiceConfig:
    db_path: str
    chunk_db_path: str | None = None
    mapping_db_path: str | None = None
    queue_db_path: str | None = None        # ReviewQueue（SME 复核）路径——非 job 队列
    manifest_db_path: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    provider_type: str = "mock"             # "mock" | "anthropic"
    model: str = DEFAULT_ANTHROPIC_MODEL
    base_url: str | None = None
    embedding: str = "none"                 # "none" | "deterministic" | "openai"
    vector_store: str = "none"              # "none" | "in_process" | "sqlite_vec"（后者需 [vector]）
    persistence_policy: str = "default"     # "default"(隔离优先) | "persist_everything"
    reference_date: str | None = None       # ISO "YYYY-MM-DD" or None
    faq_source: str | None = None           # FAQ JSON 文件路径；None -> 空缓存
    allowed_upload_root: str | None = None  # ingestion 路径必须落在此根内
    company_profile_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceConfig":
        env = os.environ if env is None else env
        return cls(
            db_path=env.get("RAGSPINE_DB_PATH", "data/fact_metric.db"),
            chunk_db_path=env.get("RAGSPINE_CHUNK_DB_PATH"),
            mapping_db_path=env.get("RAGSPINE_MAPPING_DB_PATH"),
            queue_db_path=env.get("RAGSPINE_QUEUE_DB_PATH"),
            manifest_db_path=env.get("RAGSPINE_MANIFEST_DB_PATH"),
            redis_url=env.get("RAGSPINE_REDIS_URL", "redis://localhost:6379/0"),
            provider_type=env.get("RAGSPINE_PROVIDER", "mock"),
            model=env.get("RAGSPINE_MODEL", DEFAULT_ANTHROPIC_MODEL),
            base_url=env.get("RAGSPINE_BASE_URL"),
            embedding=env.get("RAGSPINE_EMBEDDING", "none"),
            vector_store=env.get("RAGSPINE_VECTOR_STORE", "none"),
            persistence_policy=env.get("RAGSPINE_PERSISTENCE_POLICY", "default"),
            reference_date=env.get("RAGSPINE_REFERENCE_DATE"),
            faq_source=env.get("RAGSPINE_FAQ_SOURCE"),
            allowed_upload_root=env.get("RAGSPINE_ALLOWED_UPLOAD_ROOT"),
            company_profile_path=env.get("RAGSPINE_COMPANY_PROFILE"),
        )

    def reference_date_obj(self) -> date | None:
        if self.reference_date is None:
            return None
        return date.fromisoformat(self.reference_date)


def build_provider(config: ServiceConfig) -> LLMProvider:
    if config.provider_type == "mock":
        return MockProvider(reference_date=config.reference_date_obj())
    if config.provider_type == "anthropic":
        return AnthropicProvider(model=config.model, base_url=config.base_url)
    raise ValueError(f"未知 provider_type: {config.provider_type!r}")


@contextmanager
def open_fact_store(config: ServiceConfig) -> Iterator[FactStore]:
    store = FactStore(config.db_path)
    store.init_schema()
    try:
        yield store
    finally:
        store.close()


@contextmanager
def open_narrative_retriever(
    config: ServiceConfig, provider: LLMProvider
) -> Iterator[NarrativeRetriever | None]:
    if not config.chunk_db_path:
        yield None
        return
    retriever, store = build_narrative_retriever(
        config.chunk_db_path,
        provider=provider,
        embedding_backend=make_embedding_backend(config.embedding),
        vector_store=make_vector_store(config.vector_store),
        persistence_policy=make_persistence_policy(config.persistence_policy),
    )
    try:
        yield retriever
    finally:
        store.close()


class PathNotAllowedError(ValueError):
    """ingestion 路径越界或后缀不支持。"""


def validate_ingest_path(
    path: str | Path, config: ServiceConfig, *, suffixes: tuple[str, ...]
) -> Path:
    resolved = Path(path).resolve()
    if config.allowed_upload_root is not None:
        root = Path(config.allowed_upload_root).resolve()
        if not resolved.is_relative_to(root):
            raise PathNotAllowedError(f"路径不在允许根目录内: {resolved}")
    if resolved.suffix.lower() not in suffixes:
        raise PathNotAllowedError(f"不支持的文件后缀: {resolved.suffix}")
    return resolved
