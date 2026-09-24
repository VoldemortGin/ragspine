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
from enum import Enum
from pathlib import Path
from typing import Literal

from corespine import CorespineError, RateLimitedProvider, env_key, load_from_env

from ragspine.agent.agent import NarrativeRetriever
from ragspine.agent.claude_cli_provider import ClaudeCliProvider
from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    LLMProvider,
    MockProvider,
)
from ragspine.agent.query_transform import make_query_transform
from ragspine.common.observability import emit_trace
from ragspine.ingestion.page_images.render import (
    DEFAULT_PAGE_IMAGE_DPI,
    DEFAULT_PAGE_IMAGE_MAX_SIDE,
)
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.corrective import make_corrective_retriever
from ragspine.retrieval.lexical.retrieval import EmbeddingBackend
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.mode import make_retrieval_mode
from ragspine.retrieval.page_images.attach import (
    DEFAULT_PAGE_IMAGES_TOP_N,
    make_page_image_retriever,
)
from ragspine.retrieval.page_images.store import default_page_image_dir
from ragspine.retrieval.postprocess import make_postprocessor
from ragspine.retrieval.rerank.cross_encoder import make_reranker
from ragspine.retrieval.vector.chunk_index import (
    ChunkVectorIndex,
    VectorSyncReport,
    default_vector_db_path,
    embedding_model_id,
)
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.retrieval.vector.persistence_policy import make_persistence_policy
from ragspine.retrieval.vector.store import VectorStore, make_vector_store
from ragspine.storage.fact_store import FactStore, SqliteFactStore

_PACKAGED_STUDIO_DIR = Path(__file__).resolve().with_name("studio_dist")

RetrievalModeSpec = Literal["economy", "hybrid"]
EmbeddingSpec = Literal["none", "deterministic", "onnx", "local-http"]
VectorStoreSpec = Literal["none", "in_process"]
RerankerSpec = Literal["none", "cross_encoder", "local-http"]
PostprocessorSpec = Literal["none", "mmr,lost_in_middle,compress"]


class RetrievalProfile(str, Enum):
    """Named local retrieval trade-offs, from leanest to highest quality."""

    ECONOMY = "economy"
    BALANCED = "balanced"
    QUALITY = "quality"


@dataclass(frozen=True)
class RetrievalPreset:
    """Validated retrieval settings consumed by local narrative assembly."""

    retrieval_mode: RetrievalModeSpec
    embedding: EmbeddingSpec
    vector_store: VectorStoreSpec
    reranker: RerankerSpec
    postprocessor: PostprocessorSpec
    persist_vectors: bool = False
    page_parent: str = "page+child"
    page_images: str = "off"
    page_images_top_n: int = DEFAULT_PAGE_IMAGES_TOP_N
    contextual_index: str = "off"
    query_translation: str = "auto"

    def with_overrides(
        self,
        *,
        retrieval_mode: RetrievalModeSpec | None = None,
        embedding: EmbeddingSpec | None = None,
        vector_store: VectorStoreSpec | None = None,
        reranker: RerankerSpec | None = None,
        postprocessor: PostprocessorSpec | None = None,
        persist_vectors: bool | None = None,
        page_parent: str | None = None,
        page_images: str | None = None,
        page_images_top_n: int | None = None,
        contextual_index: str | None = None,
        query_translation: str | None = None,
    ) -> "RetrievalPreset":
        """Return a new preset with only explicitly supplied fields replaced."""
        return RetrievalPreset(
            retrieval_mode=retrieval_mode or self.retrieval_mode,
            embedding=embedding or self.embedding,
            vector_store=vector_store or self.vector_store,
            reranker=reranker or self.reranker,
            postprocessor=postprocessor or self.postprocessor,
            persist_vectors=self.persist_vectors if persist_vectors is None else persist_vectors,
            page_parent=page_parent or self.page_parent,
            page_images=page_images or self.page_images,
            page_images_top_n=(
                self.page_images_top_n if page_images_top_n is None else page_images_top_n
            ),
            contextual_index=contextual_index or self.contextual_index,
            query_translation=query_translation or self.query_translation,
        )


_LOCAL_RETRIEVAL_PRESETS = {
    RetrievalProfile.ECONOMY: RetrievalPreset(
        retrieval_mode="economy",
        embedding="none",
        vector_store="none",
        reranker="none",
        postprocessor="none",
    ),
    RetrievalProfile.BALANCED: RetrievalPreset(
        retrieval_mode="hybrid",
        embedding="deterministic",
        vector_store="in_process",
        reranker="none",
        postprocessor="none",
    ),
    RetrievalProfile.QUALITY: RetrievalPreset(
        retrieval_mode="hybrid",
        embedding="onnx",
        vector_store="in_process",
        reranker="cross_encoder",
        postprocessor="mmr,lost_in_middle,compress",
    ),
}


def make_retrieval_preset(
    profile: RetrievalProfile | str = RetrievalProfile.ECONOMY,
    *,
    retrieval_mode: RetrievalModeSpec | None = None,
    embedding: EmbeddingSpec | None = None,
    vector_store: VectorStoreSpec | None = None,
    reranker: RerankerSpec | None = None,
    postprocessor: PostprocessorSpec | None = None,
    persist_vectors: bool | None = None,
    page_parent: str | None = None,
    page_images: str | None = None,
    page_images_top_n: int | None = None,
    contextual_index: str | None = None,
    query_translation: str | None = None,
) -> RetrievalPreset:
    """Resolve a named local profile and apply explicit, typed overrides."""
    selected = profile if isinstance(profile, RetrievalProfile) else RetrievalProfile(profile)
    return _LOCAL_RETRIEVAL_PRESETS[selected].with_overrides(
        retrieval_mode=retrieval_mode,
        embedding=embedding,
        vector_store=vector_store,
        reranker=reranker,
        postprocessor=postprocessor,
        persist_vectors=persist_vectors,
        page_parent=page_parent,
        page_images=page_images,
        page_images_top_n=page_images_top_n,
        contextual_index=contextual_index,
        query_translation=query_translation,
    )


@dataclass(frozen=True)
class ServiceConfig:
    db_path: str
    chunk_db_path: str | None = None
    mapping_db_path: str | None = None
    queue_db_path: str | None = None  # ReviewQueue（SME 复核）路径——非 job 队列
    manifest_db_path: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    provider_type: str = (
        "mock"  # "mock" | "anthropic" | "claude-cli"(本机 `claude -p` 子进程，评测用)
    )
    model: str = DEFAULT_ANTHROPIC_MODEL
    base_url: str | None = None
    claude_cli_model: str | None = None  # claude-cli 的 --model；None=不指定（CLI 默认）
    retrieval_mode: str = "auto"  # 批次2.2④ 检索模式预设: "auto"/"hybrid"/"vector"(默认,embedding按下方配置装配,字节不变) | "economy"/"bm25"/"lexical"(零embedding成本,纯BM25关键词检索)
    embedding: str = "auto"  # "auto"(装[embed-onnx]→真语义ONNX,否则纯BM25) | "none" | "onnx" | "deterministic" | "openai" | "local-http"(/v1/embeddings,读 EMBEDDING_*)
    workflow_matcher: str = "auto"  # workflow scaffold: "auto" | "none" | "onnx"
    reranker: str = "none"  # "none"(不重排,默认行为不变) | "local-http"(/v1/rerank,读 RERANK_*) | "cross_encoder"(本地[rerank]) | "colbert"(晚交互MaxSim,[colbert]) | "splade"(学习稀疏,[splade]) | "auto"(装[rerank]即用,否则不重排)
    query_decompose: str = "none"  # W6a 查询分解(opt-in): "none"(不分解,默认字节不变) | "llm"(注入provider的LLM多跳分解)
    corrective: str = "none"  # W6b 纠错检索(opt-in): "none"(默认,返回base本身字节不变) | "crag"(有界确定性 grade→act 环)
    page_parent: str = "page+child"  # 页级父子: "page+child"(默认,按页去重+整页BM25一路再RRF) | "dedup"(按页去重,代表块带整页上下文) | "off"(检索输出与引入前字节不变)
    contextual_index: str = "off"  # 标题进索引: "off"(默认,索引文本=正文,字节不变) | "heading"(BM25/向量索引文本前拼标题路径) | "full"(再加 title/entity/period);交给 LLM 的文本不变
    query_translation: str = "auto"  # 跨语言查询翻译: "auto"(默认,问题与文档语言不一致时用 provider 译成文档语言,译文作额外的 BM25 与向量查询;精排与生成仍用原问题) | "off"(检索输出与引入前字节不变)
    page_images: str = "off"  # 图文混合上下文(opt-in): "off"(默认,prompt字节不变) | "on"(前 N 页附原 PDF 页图;需 page_parent≠off 且入库时关联了 source PDF)
    page_images_top_n: int = DEFAULT_PAGE_IMAGES_TOP_N  # page_images=on 时附图的前 N 条(页)
    page_image_dpi: int = DEFAULT_PAGE_IMAGE_DPI  # 入库渲染页图的 DPI(关联了 source PDF 时)
    page_image_max_side: int = DEFAULT_PAGE_IMAGE_MAX_SIDE  # 页图长边像素上限
    page_image_dir: str | None = None  # 页图目录;None=块库旁 page_images/
    postprocessor: str = "none"  # W8 后检索链(opt-in): "none"(默认,不挂链字节不变) | "mmr"/"lost_in_middle"/"compress" | 逗号成链如"mmr,lost_in_middle"
    query_transform: str = "none"  # W9 查询变换(opt-in,需注入provider): "none"(默认返回base字节不变) | "hyde" | "rag_fusion" | "step_back"
    adaptive: str = "none"  # W9 Adaptive-RAG 复杂度路由(opt-in): "none"(默认不路由字节不变) | "heuristic"(确定性分类) | "llm"
    chunker: str = "none"  # 批次2.2 follow-up 切块策略(ingest,opt-in): "none"(默认内置chunk_document,字节不变) | "parent_child"/"small_to_big"(父子small-to-big) | "layout"/"laws"/"qa"/"book"/"sentence_window"/"semantic"
    narrative_segment_chunking: bool = False  # 叙事入库按 segment 切块(opt-in): False(默认整篇切块,字节不变) | True(locator 带段定位如 page=N)；.md 恒按段切块
    vector_store: str = "none"  # "none" | "in_process" | "sqlite_vec"（后者需 [vector]）
    persistence_policy: str = "default"  # "default"(隔离优先) | "persist_everything"
    persist_vectors: bool = False  # 块向量持久化(opt-in): False(默认,向量库按 vector_store 装配,字节不变) | True(入库即按 embedding 嵌入写进 sqlite-vec 文件,检索读该文件;需 [vector])
    vector_db_path: str | None = None  # persist_vectors 的向量库文件；None=块库旁 <stem>.vectors.db
    reference_date: str | None = None  # ISO "YYYY-MM-DD" or None
    faq_source: str | None = None  # FAQ JSON 文件路径；None -> 空缓存
    allowed_upload_root: str | None = None  # ingestion 路径必须落在此根内
    company_profile_path: str | None = None
    tokens_per_minute: int = 0  # >0 时用 corespine RateLimitedProvider 主动 TPM 限流;0=不限
    dify_run_enabled: bool = False  # /v1/dify/run 执行开关（信任边界）；默认关，env 显式开
    dify_run_timeout_s: float = 10.0  # /v1/dify/run 单次执行超时上限（秒）
    dify_run_isolation: str = (
        "inprocess"  # "inprocess"(L1) | "subprocess"(L2，Linux setrlimit，跨平台回落 L1)
    )
    studio_dir: str = str(_PACKAGED_STUDIO_DIR)  # Studio 目录；默认 wheel 内置，""=显式禁用
    dify_public_apps: str = ""  # dify 公共 API app 注册表："key1=/path/a.yml;key2=/path/b.yml"（; 分条目、首个 = 分 key/路径）；""=未配置 -> /v1/workflows/* 一律 401
    n8n_api_key: str | None = None  # n8n 公共 API key；None=未启用，/api/v1/* 一律 401
    n8n_store_path: str = "data/n8n_store"  # n8n workflow/execution 文件存储根目录

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
    elif config.provider_type == "claude-cli":
        provider = ClaudeCliProvider(model=config.claude_cli_model)
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
        "claude_cli_model": config.claude_cli_model,
        "reference_date": config.reference_date,
        "tokens_per_minute": config.tokens_per_minute,
    }


@contextmanager
def open_fact_store(config: ServiceConfig) -> Iterator[FactStore]:
    store = SqliteFactStore(config.db_path)
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
    # 批次2.2④ 检索模式预设：economy（零 embedding 成本）显式关掉向量通道——绝不构造 embedding 后端 /
    # 向量库，纯 BM25 关键词检索。默认 'auto' = 混合模式，embedding/向量库按配置装配（字节不变）。
    mode = make_retrieval_mode(config.retrieval_mode)
    embedding_backend = make_embedding_backend(config.embedding) if mode.uses_embedding else None
    vector_index: ChunkVectorIndex | None = None
    vector_store: VectorStore | None
    if config.persist_vectors:
        # 持久化块向量（opt-in）：向量库固定为入库时写好的 sqlite-vec 文件（vector_store 不参与）；
        # 没有后端 / 库为空即明确降级为纯 BM25 并记 trace，模型不一致直接报错。
        embedding_backend, vector_index = open_vector_channel(config, embedding_backend)
        vector_store = vector_index.store if vector_index is not None else None
    else:
        vector_store = make_vector_store(config.vector_store) if mode.uses_embedding else None
    retriever, store = build_narrative_retriever(
        config.chunk_db_path,
        provider=provider,
        embedding_backend=embedding_backend,
        vector_store=vector_store,
        persistence_policy=make_persistence_policy(config.persistence_policy),
        reranker=make_reranker(config.reranker),
        postprocessor=make_postprocessor(config.postprocessor),
        page_parent=config.page_parent,
        contextual_index=config.contextual_index,
        query_translation=config.query_translation,
    )
    # W9 查询变换（opt-in，需注入 provider）：默认 "none" → make_query_transform 返回 retriever 本身
    # （字节不变）；"hyde"/"rag_fusion"/"step_back" 才包成对应 LLM 变换 wrapper。假想文档只作检索探针
    # 绝不进答案，生成变体逐个过安全门；隔离继承自 base（RESTRICTED 已在出口剔除）。
    transformed = make_query_transform(retriever, config.query_transform, provider=provider)
    # W6b 纠错检索（opt-in）：默认 "none" → make_corrective_retriever 返回 transformed 本身（字节
    # 不变）；"crag" 才包成有界确定性 grade→act 环。隔离继承自 base（RESTRICTED 已在出口剔除）。
    wrapped: NarrativeRetriever = make_corrective_retriever(transformed, config.corrective)
    # 图文混合上下文（opt-in）：默认 "off" → 原样返回（字节不变）；"on" 给最外层结果的前 N 页附页图引用，
    # 含 RESTRICTED 块的页不发图。
    wrapped = make_page_image_retriever(
        wrapped,
        config.page_images,
        chunk_db_path=config.chunk_db_path,
        image_dir=resolve_page_image_dir(config),
        top_n=config.page_images_top_n,
        page_parent=config.page_parent,
    )
    try:
        yield wrapped
    finally:
        store.close()
        if vector_index is not None:
            vector_index.close()


def resolve_page_image_dir(config: ServiceConfig) -> Path:
    """页图目录：显式 page_image_dir，否则块库旁 ``page_images/``。"""
    if config.page_image_dir:
        return Path(config.page_image_dir)
    return default_page_image_dir(config.chunk_db_path or config.db_path)


def resolve_vector_db_path(config: ServiceConfig) -> Path:
    """persist_vectors 的向量库文件：显式 vector_db_path，否则块库旁 ``<stem>.vectors.db``。"""
    if config.vector_db_path:
        return Path(config.vector_db_path)
    return default_vector_db_path(config.chunk_db_path or config.db_path)


def open_vector_channel(
    config: ServiceConfig, embedding_backend: EmbeddingBackend | None
) -> tuple[EmbeddingBackend | None, ChunkVectorIndex | None]:
    """检索期打开持久化向量库；返回 (后端, 索引)，降级时两者皆 None。每次都发一条计数 trace。

    索引由调用方 close（open_narrative_retriever 在退出时关）；模型标识或索引文本版本（contextual_index）
    不一致抛 VectorIndexMismatchError。
    """
    path = resolve_vector_db_path(config)
    reason = ""
    index: ChunkVectorIndex | None = None
    if embedding_backend is None:
        reason = "no_embedding_backend"
    elif not path.is_file():
        reason = "empty_index"
    else:
        index = ChunkVectorIndex(path)
        try:
            index.check_compatible(
                embedding_model_id(embedding_backend), contextual_index=config.contextual_index
            )
        except Exception:
            index.close()
            raise
        if index.count() == 0:
            index.close()
            index = None
            reason = "empty_index"
    n_vectors = index.count() if index is not None else 0
    emit_trace(
        op="narrative.vector_channel",
        vector_channel="hybrid" if index is not None else "bm25_only",
        vector_reason=reason,
        n_vectors=n_vectors,
    )
    if index is None:
        return None, None
    return embedding_backend, index


def index_narrative_vectors(config: ServiceConfig) -> VectorSyncReport | None:
    """入库后把块库同步进持久化向量库（persist_vectors 打开时）；开关关闭返回 None。

    按 config 装配 embedding 后端（economy 模式 / 无后端即降级为纯 BM25，不建向量文件）；持久化策略默认
    隔离优先（RESTRICTED 块不嵌入）；doc 粒度幂等，模型标识不一致抛 VectorIndexMismatchError。
    每次都发一条只含计数的 trace（op=narrative.vector_index）。
    """
    if not config.persist_vectors:
        return None
    chunk_store = ChunkStore(config.chunk_db_path or config.db_path)
    try:
        chunk_store.init_schema()
        chunks = chunk_store.iter_chunks()
    finally:
        chunk_store.close()
    mode = make_retrieval_mode(config.retrieval_mode)
    backend = make_embedding_backend(config.embedding) if mode.uses_embedding else None
    if backend is None:
        report = VectorSyncReport(
            vector_channel="bm25_only",
            vector_reason="no_embedding_backend" if mode.uses_embedding else "retrieval_mode",
            n_chunks=len(chunks),
        )
    else:
        index = ChunkVectorIndex(resolve_vector_db_path(config))
        try:
            report = index.sync(
                chunks,
                backend,
                model_id=embedding_model_id(backend),
                persistence_policy=make_persistence_policy(config.persistence_policy),
                contextual_index=config.contextual_index,
            )
        finally:
            index.close()
    emit_trace(
        None,
        op="narrative.vector_index",
        vector_channel=report.vector_channel,
        vector_reason=report.vector_reason,
        **report.counts(),
    )
    return report


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
