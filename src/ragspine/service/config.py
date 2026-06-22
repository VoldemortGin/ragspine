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

from corespine import CorespineError, env_key, load_from_env

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

    # 历史 env 键别名 -> 字段名。corespine load_from_env 默认按 PREFIX_FIELDNAME
    # 推导键名，与这三个不规则旧键冲突；构造时把旧键改写到规范键以保持向后兼容。
    _ENV_PREFIX = "RAGSPINE"
    _LEGACY_ENV_ALIASES = {
        "RAGSPINE_PROVIDER": "provider_type",
        "RAGSPINE_COMPANY_PROFILE": "company_profile_path",
        "RAGSPINE_FAQ_SOURCE": "faq_source",
    }
    # db_path 字段无 dataclass 默认值，from_env 历史上在此层兜底为该路径。
    _DB_PATH_FALLBACK = "data/fact_metric.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceConfig":
        """从 RAGSPINE_* 环境变量装配配置。

        规则字段交由 corespine load_from_env（PREFIX_FIELDNAME）；三个不规则旧键
        先改写到规范键名再交给它，旧键名仍向后兼容。规范键存在时优先于旧别名。
        db_path 字段无 dataclass 默认值，缺失时在此层兜底为历史默认路径。
        """
        env = os.environ if env is None else env
        normalized = dict(env)
        for legacy_key, field_name in cls._LEGACY_ENV_ALIASES.items():
            canonical_key = env_key(cls._ENV_PREFIX, field_name)
            if legacy_key in normalized and canonical_key not in normalized:
                normalized[canonical_key] = normalized[legacy_key]
        normalized.setdefault(env_key(cls._ENV_PREFIX, "db_path"), cls._DB_PATH_FALLBACK)
        return load_from_env(cls, prefix=cls._ENV_PREFIX, env=normalized)

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


class PathNotAllowedError(CorespineError):
    """ingestion 路径越界或后缀不支持。

    继承家族统一异常基类，稳定 code 为 "config.path_not_allowed"（ADR errors 缝）。
    """

    code = "config.path_not_allowed"


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
