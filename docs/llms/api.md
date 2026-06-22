# api —— 使用者主要入口的真实签名 + 契约

> 所有签名经 `inspect.signature` 对真实代码核对（ragspine 0.1.2）。按子系统分节。深层结构：
> 顶层 `import ragspine` 只 curated 暴露 4 个名字（`FactStore` / `Fact` / `MockProvider` /
> `answer_question`），其余按惰性子模块点到底（如 `ragspine.retrieval.lexical.retrieval.HybridRetriever`）。

## 0 · 顶层 curated（最小可用 API）

```python
from ragspine import FactStore, Fact, MockProvider, answer_question
```

`import ragspine` 不急切 import 任何子模块；首次访问某名字才拉起其源模块（PEP 562 惰性）。
缺可选 extra 不影响 `import ragspine`，只有真正用到的那条链才拉对应依赖。

---

## 1 · agent（编排入口）

模块 `ragspine.agent.agent` / `ragspine.agent.llm_provider`。

```python
def answer_question(
    question: str,
    store: FactStore,
    provider: LLMProvider,
    *,
    reference_date: date | None = None,
    narrative_retriever: NarrativeRetriever | None = None,
    intent_parser: IntentParser | None = None,
) -> AgentResult
```
单条问题端到端编排。`reference_date` 是相对期间（去年 / 上半年）的换算基准，默认今天；
`narrative_retriever` 缺省时叙事路坦白降级；`intent_parser` 缺省用零-LLM 规则实现。

```python
@dataclass
class AgentResult:
    answer: str
    route: str                                # "structured" | "narrative" | "composite" | ...
    clarification: ClarificationResult | None = None
    tool_results: list[dict[str, object]] = ...
    sources: list[dict[str, object]] = ...    # [{"doc": ..., "locator": ...}]
```

**LLMProvider 协议**（注入点，核心零 SDK）：
```python
@runtime_checkable
class LLMProvider(Protocol):
    def create_message(
        self, *, system: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ProviderResponse: ...
```

实现：
- `MockProvider(reference_date: date | None = None)` —— 离线确定性，无 key、无网络。
- `AnthropicProvider(api_key=None, model="claude-opus-4-8", base_url=None, max_tokens=16000, timeout=30.0, max_retries=2)`
  —— 真实 Claude；SDK 延迟 import（需 `[llm]`）；`base_url` 可覆盖（企业网关）。

```python
@dataclass
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall] = ...
    stop_reason: str = "end_turn"
    raw_content: list[object] = ...
    usage: dict[str, int | None] | None = None   # MockProvider 即 None

class ProviderError(CorespineError):              # code = "provider.error"
    ...                                           # 只包裹 SDK 网络/API 异常；程序错误照常上抛
```
常量：`DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"`、`NARRATIVE_PROMPT_PREFIX`（叙事合成固定前缀）。

---

## 2 · storage（结构化事实表）

模块 `ragspine.storage.fact_store`。

```python
@dataclass
class Fact:
    metric_code: str; entity: str; geography: str; channel: str
    period_type: str; period: str; value: float; unit: str
    source_doc_id: str; source_locator: str          # <- provenance（必填）
    # v2 可选扩展（默认保持旧行为）：tags / source_file_hash / extractor_version /
    # mapping_version / confidence / review_status / valid_as_of / ingested_at /
    # corrected_by / corrected_audit_seq / dimensions
```

```python
class FactStore:
    def __init__(self, db_path: str | Path): ...      # ":memory:" 或文件路径
    def init_schema(self) -> None: ...                # 首次必调
    def upsert_facts(self, facts: list[Fact], ingested_at: str | None = None) -> int: ...
    def query(self, ...) -> ...: ...                  # 按维度查（agent 内部经 query_metric 工具用）
    def close(self) -> None: ...
```
契约：自然键去重（channel/entity/metric/period 身份维度，geography 不入键）；upsert 幂等。

---

## 3 · retrieval（叙事 RAG）

### 3.1 chunking —— `ragspine.retrieval.chunking.chunking`

```python
@dataclass
class DocumentMeta:
    doc_id: str; title=""; topic=""; entity=""; geography=""
    period=""; language=""; sensitivity="INTERNAL"; source_locator_prefix=""

def chunk_document(
    text: str, meta: DocumentMeta, *,
    max_chars: int = 480, overlap_chars: int = 80,
) -> list[Chunk]                                       # Chunk.chunk_id = "{doc_id}#c{seq}"
```

### 3.2 lexical —— `ragspine.retrieval.lexical.retrieval`

```python
class HybridRetriever:
    def __init__(
        self,
        chunks: Sequence[StoredChunk | Chunk],
        *,
        embedding_backend: EmbeddingBackend | None = None,   # None = 纯 BM25 模式
        query_rewriter: QueryRewriter | None = None,
        k1: float = 1.5, b: float = 0.75,
        rrf_k: float = 60, top_k: int = 50,
        embedding_cache: dict[str, list[float]] | None = None,
        vector_store: VectorStore | None = None,             # 缺省：有 embedding 后端时自建 InProcessVectorStore
        manage_vectors: bool = True,
    ): ...

    def search(
        self, query: str, *,
        topic: str | None = None, entity: str | None = None,
        geography: str | None = None, period: str | None = None,
        language: str | None = None, top_k: int | None = None,
    ) -> list[RetrievalResult]: ...                          # RRF 序，平分按 chunk_id 升序

    def topology(self) -> PipelineGraph: ...
```

```python
@dataclass
class RetrievalResult:
    chunk: StoredChunk | Chunk
    bm25_score: float; vector_score: float; fused_score: float   # 排序依据 = fused_score
```

注入协议（零 SDK）：
```python
class EmbeddingBackend(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
class QueryRewriter(Protocol):
    def rewrite(self, query: str) -> list[str]: ...      # 须含原 query
```

纯函数（独立可用）：`tokenize(text)`、`bm25_scores(query_tokens, docs_tokens, k1=1.5, b=0.75)`、
`cosine_similarity(a, b)`、`rrf_fuse(rankings, k=60)`。默认改写器 `GlossaryQueryRewriter(max_queries=5)`。

**NarrativeIndex**（端到端：建库 -> 检索 -> 可选精排）：
```python
class NarrativeIndex:
    def __init__(self, store: ChunkStore, *,
                 embedding_backend=None, query_rewriter=None, judge=None,
                 max_chars=480, overlap_chars=80, top_k=50, rerank_top_n=...,
                 vector_store=None, persistence_policy=None): ...
    def ingest(self, text: str, meta: DocumentMeta, valid_as_of: str = "") -> int: ...
    def retrieve(self, query: str, *, topic=None, entity=None, geography=None,
                 period=None, language=None, top_k=None, rerank=True, top_n=None
                 ) -> list[RetrievalResult]: ...
```

### 3.3 vector —— `ragspine.retrieval.vector`

```python
# store.py
@dataclass(frozen=True)
class VectorRecord:
    id: str; vector: tuple[float, ...]; metadata: Mapping[str, str] = ...
@dataclass(frozen=True)
class VectorHit:
    id: str; score: float; metadata: Mapping[str, str]

class VectorStore(Protocol):                              # core 只 import 这个 Protocol
    def upsert(self, records: Sequence[VectorRecord]) -> int: ...
    def query(self, vector: Sequence[float], *, k: int = 50,
              where: Mapping[str, str] | None = None) -> list[VectorHit]: ...
    def delete(self, *, where: Mapping[str, str]) -> int: ...
    def count(self) -> int: ...

class InProcessVectorStore: ...                           # 零依赖确定性默认

def make_vector_store(spec: str | None = None, **kwargs) -> VectorStore | None
# spec（大小写/留白不敏感；缺省读 RAGSPINE_VECTOR_STORE）：
#   None/'none' -> None（检索器用内置默认）
#   'in_process'/'in-process'/'memory' -> InProcessVectorStore
#   'sqlite_vec' / 'pgvector' / 'qdrant' -> 对应 adapter（需 [vector]，延迟 import）
#   其余 -> entry-point group 'ragspine.vector_stores' 自动发现；都不命中 -> ValueError
```
注意：`make_vector_store` 是 **ragspine 自己的 entry-point 发现**，**未**折进 corespine 的 `Registry`。

```python
# embedding_backends.py
def make_embedding_backend(spec: str | None = None, **kwargs) -> EmbeddingBackend | None
# 薄封装 corespine.Registry（EMBEDDING_BACKENDS）+ lazy_extra_import。
# spec（大小写/留白/连字符不敏感；缺省读 RAGSPINE_EMBEDDING_BACKEND）：
#   None/'none' -> None（纯 BM25）
#   'deterministic' -> DeterministicEmbeddingBackend(dim=256)（离线词法散列，非语义）
#   'openai' -> OpenAIEmbeddingBackend（延迟 import，需 [llm]）
#   'qwen3' / 'sentence-transformers' / 'st' -> SentenceTransformerEmbeddingBackend（需 [embed]；设备 cuda/mps/cpu 自适应）

class DeterministicEmbeddingBackend:
    def __init__(self, dim: int = 256): ...
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

# persistence_policy.py —— 决定一个块的向量是否可写盘（at-rest）
class PersistencePolicy(Protocol):
    def persistable(self, chunk: StoredChunk | Chunk) -> bool: ...
def make_persistence_policy(spec: str | None = None) -> PersistencePolicy
#   None/'default'/'isolation_first' -> IsolationFirstPolicy（绝不落盘 RESTRICTED 向量，安全默认）
#   'persist_everything'/'all'       -> PersistEverythingPolicy（opt-in）
```

### 3.4 link —— `ragspine.retrieval.link.narrative_link`（接线入口）

```python
def build_narrative_retriever(
    chunk_db: str | Path,
    provider: LLMProvider | None = None,
    *,
    embedding_backend: EmbeddingBackend | None = None,
    vector_store: VectorStore | None = None,
    persistence_policy: PersistencePolicy | None = None,
) -> tuple[NarrativeIndexRetriever, ChunkStore]              # store 由调用方 close
```
默认链：纯 BM25+RRF + glossary multi-query +（给了 provider 时）Claude listwise 二审。
适配器在出口剔除 `sensitivity == RESTRICTED` 的块（不出域）。

---

## 4 · extraction（文档 -> StyledGrid IR）

模块 `ragspine.extraction.*`。抽取器把 office 文档（xlsx / pptx / 数字 PDF / 扫描 PDF）解析成
冻结的 `StyledGrid` 中间表示（style- & color-aware），再交 ingestion 落库。重依赖延迟 import：
数字 PDF 需 `[pdf]`（docling），扫描 PDF OCR 需 `[ocr]`（paddleocr，Linux + NVIDIA GPU）。
OCR 后端是注入式 `OcrBackend` 协议。子域：`extractors/` `routing/`（PDF 分诊）`color/` `verification/`。

---

## 5 · service（HTTP + 异步队列层，需 [service]）

### 5.1 config —— `ragspine.service.config`

```python
@dataclass(frozen=True)
class ServiceConfig:
    db_path: str
    chunk_db_path: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    provider_type: str = "mock"          # "mock" | "anthropic"
    model: str = DEFAULT_ANTHROPIC_MODEL
    base_url: str | None = None
    embedding: str = "none"              # "none" | "deterministic" | "openai" | ...
    vector_store: str = "none"          # "none" | "in_process" | "sqlite_vec" | ...
    persistence_policy: str = "default"
    reference_date: str | None = None
    faq_source: str | None = None
    allowed_upload_root: str | None = None
    company_profile_path: str | None = None
    # ...（mapping_db_path / queue_db_path / manifest_db_path）

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceConfig": ...
```
`from_env` 用 corespine `load_from_env`（`RAGSPINE_FIELDNAME` -> 字段）；三个旧别名
（`RAGSPINE_PROVIDER` / `RAGSPINE_COMPANY_PROFILE` / `RAGSPINE_FAQ_SOURCE`）向后兼容。
`db_path` 缺失时兜底 `data/fact_metric.db`。

辅助：`build_provider(config)`、`open_fact_store(config)`（contextmanager）、
`open_narrative_retriever(config, provider)`（contextmanager）、
`validate_ingest_path(path, config, *, suffixes)`（越界/坏后缀抛 `PathNotAllowedError`）。

```python
class PathNotAllowedError(CorespineError):    # code = "config.path_not_allowed"
    ...
```

### 5.2 tasks —— `ragspine.service.tasks.task_queue`

```python
class TaskQueue(corespine.TaskQueue, Protocol):     # 扩展 corespine 中性核 enqueue/get
    def enqueue(self, func: JobFunc | str, payload: dict[str, Any], *,
                job_id=None, timeout=None, max_retries=0,
                result_ttl=None, failure_ttl=None) -> str: ...
    def get(self, job_id: str) -> JobStatus | None: ...

class FakeQueue:    # 同步内联执行（测试用，无需 Redis），签名同上
class RQQueue:      # RQ+Redis 后端，rq/redis 延迟 import

class JobError(CorespineError):                      # code = "job.error"
    def __init__(self, message: str, *, stage="execution", retryable=False): ...
```
`JobStatus`（id/status/result/error）直接复用 corespine；`error_to_dict` 归一异常。
状态常量：`JOB_QUEUED` / `JOB_STARTED` / `JOB_FINISHED` / `JOB_FAILED`。`map_rq_status(rq_status)`。

### 5.3 api —— `ragspine.service.api`（FastAPI app）

端点：`GET /healthz`、`GET /readyz`、`POST /v1/ask`、`POST /v1/ingest/structured/jobs`、
`POST /v1/ingest/narrative/jobs`、`GET /v1/jobs/{job_id}`。路由是薄适配层：schema / DI /
资源装配 / FAQ 短路 / 错误整形 / trace —— 一律调用既有 workflow，不重写逻辑。

---

## 6 · observability —— `ragspine.common.observability`

```python
def new_request_id() -> str                          # uuid4 hex 12 位
def emit_trace(logger: logging.Logger | None = None, **fields: object) -> None
```
`emit_trace` 先过 corespine `InProcessPrivacyTraceSink`：载荷含禁词键（answer / value / text /
content / prompt / completion / chunk / chunk_text / body，大小写不敏感、精确键名）会**直接抛
`TraceError`、绝不落盘**。只应传非敏感元数据（route / 状态计数 / 分数 / 耗时 / token 用量）。
