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

from corespine import CorespineError, RateLimitedProvider, env_key, load_from_env

from ragspine.agent.agent import NarrativeRetriever
from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    LLMProvider,
    MockProvider,
)
from ragspine.agent.query_transform import make_query_transform
from ragspine.retrieval.corrective import make_corrective_retriever
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.postprocess import make_postprocessor
from ragspine.retrieval.rerank.cross_encoder import make_reranker
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
    embedding: str = "auto"                 # "auto"(装[embed-onnx]→真语义ONNX,否则纯BM25) | "none" | "onnx" | "deterministic" | "openai"
    reranker: str = "none"                  # "none"(不重排,默认行为不变) | "cross_encoder"(本地[rerank]) | "colbert"(晚交互MaxSim,[colbert]) | "splade"(学习稀疏,[splade]) | "auto"(装[rerank]即用,否则不重排)
    query_decompose: str = "none"           # W6a 查询分解(opt-in): "none"(不分解,默认字节不变) | "llm"(注入provider的LLM多跳分解)
    corrective: str = "none"                 # W6b 纠错检索(opt-in): "none"(默认,返回base本身字节不变) | "crag"(有界确定性 grade→act 环)
    postprocessor: str = "none"              # W8 后检索链(opt-in): "none"(默认,不挂链字节不变) | "mmr"/"lost_in_middle"/"compress" | 逗号成链如"mmr,lost_in_middle"
    query_transform: str = "none"            # W9 查询变换(opt-in,需注入provider): "none"(默认返回base字节不变) | "hyde" | "rag_fusion" | "step_back"
    adaptive: str = "none"                   # W9 Adaptive-RAG 复杂度路由(opt-in): "none"(默认不路由字节不变) | "heuristic"(确定性分类) | "llm"
    vector_store: str = "none"              # "none" | "in_process" | "sqlite_vec"（后者需 [vector]）
    persistence_policy: str = "default"     # "default"(隔离优先) | "persist_everything"
    reference_date: str | None = None       # ISO "YYYY-MM-DD" or None
    faq_source: str | None = None           # FAQ JSON 文件路径；None -> 空缓存
    allowed_upload_root: str | None = None  # ingestion 路径必须落在此根内
    company_profile_path: str | None = None
    tokens_per_minute: int = 0               # >0 时用 corespine RateLimitedProvider 主动 TPM 限流;0=不限
    dify_run_enabled: bool = False           # /v1/dify/run 执行开关（信任边界）；默认关，env 显式开
    dify_run_timeout_s: float = 10.0         # /v1/dify/run 单次执行超时上限（秒）
    dify_run_isolation: str = "inprocess"    # "inprocess"(L1) | "subprocess"(L2，Linux setrlimit，跨平台回落 L1)

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
    provider: LLMProvider
    if config.provider_type == "mock":
        provider = MockProvider(reference_date=config.reference_date_obj())
    elif config.provider_type == "anthropic":
        provider = AnthropicProvider(model=config.model, base_url=config.base_url)
    else:
        raise ValueError(f"未知 provider_type: {config.provider_type!r}")
    # 主动 TPM 限流(可选):tokens_per_minute>0 时用 corespine RateLimitedProvider 包装,
    # 与 SDK 自带 max_retries 的被动退避互补(两层)。
    if config.tokens_per_minute > 0:
        return RateLimitedProvider(provider, tokens_per_minute=config.tokens_per_minute)
    return provider


def provider_config_dict(config: ServiceConfig) -> dict[str, object]:
    """抽出 provider 重建所需的纯可序列化字段（供 dify 子进程 / worker 自建 provider）。

    刻意只含 provider 配置，绝不含 provider 实例 / provider_expr——隔离进程 / worker 用
    build_provider 从这些字段重建，确保 provider 始终由服务端 env 决定、客户端不可注入。
    """
    return {
        "provider_type": config.provider_type,
        "model": config.model,
        "base_url": config.base_url,
        "reference_date": config.reference_date,
        "tokens_per_minute": config.tokens_per_minute,
    }


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
        reranker=make_reranker(config.reranker),
        postprocessor=make_postprocessor(config.postprocessor),
    )
    # W9 查询变换（opt-in，需注入 provider）：默认 "none" → make_query_transform 返回 retriever 本身
    # （字节不变）；"hyde"/"rag_fusion"/"step_back" 才包成对应 LLM 变换 wrapper。假想文档只作检索探针
    # 绝不进答案，生成变体逐个过安全门；隔离继承自 base（RESTRICTED 已在出口剔除）。
    transformed = make_query_transform(retriever, config.query_transform, provider=provider)
    # W6b 纠错检索（opt-in）：默认 "none" → make_corrective_retriever 返回 transformed 本身（字节
    # 不变）；"crag" 才包成有界确定性 grade→act 环。隔离继承自 base（RESTRICTED 已在出口剔除）。
    wrapped: NarrativeRetriever = make_corrective_retriever(transformed, config.corrective)
    try:
        yield wrapped
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
