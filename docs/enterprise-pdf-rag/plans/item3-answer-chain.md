# 第 3 项设计：自然语言检索回答链（hybrid 检索 → 上下文 → 合成 → 逐字段校验 → 拒答 → OpenAI-compatible chat）

仓库 `/Users/linhan/startup/spine/ragspine`，分支 `feat/generic-document-service`，HEAD `b91205e`。只读设计，不改仓库。
所有路径相对 `src/enterprise_pdf_rag/`，除非写明 `ragspine/`。

---

## 0. 调研纠偏（与任务描述不一致、且影响设计的事实）

| 假设 | 实际 | 影响 |
|---|---|---|
| `ProcessingRetrieval.search(query, top_k)` / `resolve(hit)` | `search(publication, query, *, limit=5)`、`resolve(publication, hit)`（`adapters/processing_retrieval.py:234/265`）；hit=`PinnedRetrievalHit(snapshot_id, member_id, score)`（`processing/retrieval.py:99`）；context=`RetrievalContext(snapshot_id, member, ir, description, qualification)`（`processing/retrieval.py:143`） | 第 2 项的 `MountedDocument` 负责把 `publication` 绑定进去；第 3 项只见 `(query, limit)` / `(hit)` |
| 存在 `AbstainReason` | 不存在。有 `RefusalReason`（donut，`figures/chart_qa/models.py:31`）、`DisplayedRefusalReason`（bar，`displayed_models.py:34`）、硬错误 `QueryFailure{PIN_CONFLICT, INVALID_EVIDENCE, UNAVAILABLE_EVIDENCE}` | 新建 `answers.AbstainReason` 做超集，提供两枚举的映射函数 |
| `JsonCompletionClient.complete_json` 可做纯文本 | 强制 `image_png` 且校验 PNG magic（`adapters/json_completion.py:215-249`） | 需新增纯文本入口 `complete_text_json`（最小改动，见 §6） |
| 描述文本有单一字段 | 文字/列表/组：`ObjectDescription.text`（`processing/typed_ir.py:106`）；图表：`TextDescription.text` 是 property（`figures/models.py:317`，claims 文本空格拼接）。索引用的正是 `checked_description.text`（`processing_retrieval.py:158`） | BM25 语料 = 每个成员的 `description.text`，与向量通道嵌入的文本完全同一 |
| TABLE 成员可检索 | `eligibility()` 只收 `TEXT/LIST/GROUP/CHART`（`processing_retrieval.py:53-60`）；`validate_literal_member` 返回 `TextIR | ListIR | GroupIR`（`adapters/literal_qualification.py:22-27`）。`TableIR` 由 `adapters/pdfspine_tables.py:190` 产出（`find_tables(strategy="lines")`），但从不进快照 | 表格验收必须先放行 TABLE 成员（§10 阶段 4；§12 拍板点 3） |
| enterprise_pdf_rag 可在任意位置 import ragspine | `scripts/enterprise_pdf_rag/check_conformance.py:215-233`：`adapters/` 之外 import 面是封闭白名单（stdlib + 本包 + `[project.dependencies]` 的 import 名）。`ragspine` 同发行版但**不在** dependencies → **只能在 `adapters/` 下 import `ragspine`**；目前无先例 | hybrid/RRF 包装必须放 `adapters/hybrid_search.py`；`processing/`、`answers/` 保持纯 stdlib+pydantic |

---

## 1. 复用清单（精确路径与函数）

### 1.1 ragspine 侧（只在 `adapters/` 下 import；全部纯 stdlib、零 corespine）

| 复用件 | 路径 | 签名 | 用途 |
|---|---|---|---|
| `tokenize` | `ragspine/retrieval/lexical/retrieval.py` | `tokenize(text: str) -> list[str]`（小写；ASCII 词；CJK unigram+bigram） | BM25 语料与查询分词（tokenizer 不可注入，直接用） |
| `bm25_scores` | 同上 | `bm25_scores(query_tokens, docs_tokens, k1=1.5, b=0.75) -> list[float]` | 词法通道打分 |
| `rrf_fuse` | 同上 | `rrf_fuse(rankings: list[list[str]], k: float = 60) -> dict[str, float]` | 输入两份**已排序 id 列表**，正是"自带向量排序 + BM25 排序"的融合入口 |
| `ListwiseJudge` / `listwise_rerank` | `ragspine/retrieval/rerank/listwise_rerank.py` | `listwise_rerank(query, results: Sequence[Any], judge: ListwiseJudge | None, *, top_n=10) -> list[Any]`；元素需有 `.chunk.text`、`.chunk.sensitivity`；`judge=None` = 确定性 identity 截断 | 可选 rerank 开关，默认 `None` |
| （可选）`faithfulness` / `LexicalOverlapJudge` | `ragspine/eval/groundedness.py` | `faithfulness(answer, context_texts, judge=None) -> FaithfulnessResult(.ok)` | 对回答散文的第二道确定性 grounding 门（默认关） |

**不复用**（原因）：
- `HybridRetriever`（`retrieval.py:223`）：`embedding_backend=None` 可纯 BM25，但 import 连带 chunking/filtering/vector/pipeline 一串模块，且要求 `Chunk/StoredChunk` 形态、无"自带向量排序"参数——只要三个纯函数就够。
- `ragspine/agent/agent.py::answer_question`：绑 `FactStore` + 意图三路分流，无 claims/citations 结构；其 anti-fabrication 不变量（模型散文永不信数字、no-found 强制改写拒答）在本包用逐字段校验实现（§7）。
- `ragspine/service/api/openai_public.py`：与 `ServiceConfig`/FAQ 绑定；其"守卫链先跑完再回放 SSE"的纪律本包 `adapters/http/openai_demo.py::completion_events` 已等价实现，直接用本包的。
- `ragspine/retrieval/rerank/cross_encoder.py::make_reranker`：拉入 corespine + fastembed；本包已有 `adapters/local_models.py::LocalRerankAdapter.rerank(query, documents, *, limit)`，用它做 `ListwiseJudge` 适配即可。

### 1.2 enterprise_pdf_rag 既有件（直接用，不改）

- 检索/水化：`ProcessingRetrieval.search/resolve`（经第 2 项 `MountedDocument`）；`RetrievalContext`、`PinnedRetrievalHit`、`RetrievalMember`、`RetrievalPlan.snapshot_id`。
- IR：`TextIR/ListIR/GroupIR.fragments: tuple[ObservedText(source_span_id, text, source)]`（`processing/typed_ir.py:16-46`）；`TableIR(cells: TableCell(cell_id,row,col,row_span,col_span,bbox,source_span_ids,text,content_state), slots, row_count, col_count)`（`processing/table_models.py:23-88`）；`ChartIR(binding, grammar, axes, points: ChartPoint(point_id, series/category/unit: TextField(text, evidence), value: NumericObservation(value: Decimal|None, kind: ValueKind, evidence)), title, period, marks)`（`figures/models.py:219-290`）。
- 图表逐字段校验：`figures/chart_qa/evidence.py::check_fields(context)`、`citation(context, path, evidence) -> FieldCitation`、`source_display(context, point) -> str`（Decimal 相等 + SVG 原文回读）；`figures/chart_qa/service.py::_precision_supported(value)`；bar：`displayed_evidence.py::chart_context(DisplayedLookupContext) -> ChartContext`、`check_displayed_evidence`。
- 图表上下文解析器：`adapters/chart_qa.py::StoredChartResolver(sources, outputs, *, processing_id).resolve(pin: QueryPin) -> ChartContext`；`adapters/chart_qa_displayed.py::StoredDisplayResolver.resolve(pin) -> DisplayedLookupContext`。`QueryPin(processing_id, snapshot_id, member_id)`（`figures/chart_qa/models.py:63`）。
- LLM：`adapters/json_completion.py::JsonCompletionClient(config, *, cache_dir, max_live_calls, timeout, sender, retry_failed)`、`JsonCompletionResult[T]`、`JsonCompletionError`；`adapters/providers.py::_send_once`（`SmokeSender` 协议）、`load_llm_config() -> LLMConfig`。
- HTTP：`adapters/http/openai_schemas.py::{ChatMessage, StreamOptions, CompletionResponse, CompletionChoice, CompletionChunk}`；`adapters/http/openai_demo.py::completion_events(result)`（SSE 切片回放）；`adapters/http/webui_gate.py::source_review_page(content)`；`adapters/http/aia_review.py` 来源审阅路径；错误码先例 409/422/503（`processing_review.py:144-251`, `chart_qa.py:53`）。
- 测试基建：`tests/enterprise_pdf_rag/adapters/bar_source_fixture.py::bar_source()`、`chart_qa_bar_fixture.py::published_bar_input(tmp_path)`、`test_chart_qa_store.py::published_chart(tmp_path)`（donut 已发布快照）、`test_pdf_ingestion.py::authored_pdf(path, *, page_count, label)`、`test_pdfspine_tables.py::_table_pdf()/_page_input()`、`test_generic_publication_e2e.py::_text_partition_sender` + `monkeypatch.setattr("enterprise_pdf_rag.adapters.json_completion._send_once", ...)`、`processing/test_persistent_retrieval.py::RecordingEmbedding`。

---

## 2. 架构与数据流

```
AnswerRequest(question, document?)
  │  DocumentCatalog.mount(sha) -> MountedDocument            [第 2 项]
  ▼
adapters/hybrid_search.py::HybridSearch.search(query)
  ├─ 向量通道: document.search(query, limit=L)  -> ranked member_ids (cosine, pinned snapshot)
  ├─ 词法通道: LexicalIndex(snapshot_id) 上 bm25_scores(tokenize(query)) -> ranked member_ids
  └─ rrf_fuse([[vec...],[lex...]], k=60) -> FusedHit[]  (可选 listwise_rerank, 默认 None)
  ▼
document.resolve(hit) -> RetrievalContext            (无模型；重校验存储证据；坏证据 -> ValueError/ChartQueryError)
processing/context_builder.py::build_context_block(context) -> ContextBlock   (文字: 逐字 span；表格: 网格+cell；图表: ChartIR 字段+element_ids)
  ▼
answers/prompt.py::build_prompt(question, blocks) ; adapters/answer_service.py -> JsonCompletionClient.complete_text_json(..., response_model=ModelAnswer)   [恰好一次 LLM 调用]
  ▼
answers/verify.py::verify_claims(model_answer, blocks, chart_contexts) -> verified / rejected
answers/verify.py::prose_grounded(answer_text, verified) -> bool                (散文里的数字必须来自已验证 claim)
  ▼
AnswerResult(ANSWERED | ABSTAINED + AbstainReason)  ->  adapters/http/chat.py (200 / 409 / 503 / 422; SSE 回放)
```

不变量落点：
- 索引里只有 description 文本（向量 + BM25 同一语料）；ChartIR/SVG/显示值只在 `resolve`/`chart_context` 时取出（§4）。
- 每次请求 pin 一个 `retrieval_snapshot_id`；`LexicalIndex` 与该 id 绑定；不改快照。
- 无隐式模型调用：向量通道走第 2 项 `MountedDocument.search`（embedder 显式注入，缺失 → 503）；rerank 默认 `None`；LLM 恰好一次且走 `JsonCompletionClient` 缓存/预算。
- 拒答=200 业务结果；证据损坏=409；依赖缺失=503；输入不变量=422（PRD §5）。

---

## 3. 与第 2 项的接口（`MountedDocument` 最小 Protocol）

放 `answers/ports.py`（纯；被 `adapters/answer_service.py`、`adapters/hybrid_search.py` 与测试消费；第 2 项的真实实现放 `adapters/document_catalog.py`）。

```python
@dataclass(frozen=True, slots=True)
class MemberText:
    member_id: str
    kind: ObjectKind
    page_index: int
    text: str                      # 与 IndexEntry 向量对应的、被嵌入的 description.text（原文，不规整）

@runtime_checkable
class MountedDocument(Protocol):
    @property
    def source_sha256(self) -> str: ...
    @property
    def processing_id(self) -> str: ...            # = current_processing_id；构造 QueryPin 必需
    @property
    def retrieval_snapshot_id(self) -> str: ...
    @property
    def embedding_fingerprint(self) -> str: ...
    def member_texts(self) -> tuple[MemberText, ...]: ...                    # BM25 语料；按 member_id 排序；只读 plan+description 资产，不做证据校验、不调模型
    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]: ...   # -> ProcessingRetrieval.search(publication, query, limit=limit)
    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext: ...                  # -> ProcessingRetrieval.resolve(publication, hit)
    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext: ...                # -> StoredChartResolver(..., processing_id).resolve(QueryPin(...))
    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext: ...  # -> StoredDisplayResolver(...).resolve(QueryPin(...))（bar v2 成员）
```

- 相对任务书中"`search`/`resolve` 两方法"的**新增三项**：`member_texts()`（BM25 无此拿不到语料）、`chart_context()`/`displayed_context()`（`citation`/`source_display` 需要 `ChartContext.svg: SvgArtifact`，`RetrievalContext.member.source_svg` 只是 `AssetRef`，第 3 项不碰 store）。→ §12 拍板点 1。
- 异常约定（第 2 项实现须遵守，第 3 项据此映射 HTTP）：embedder 未配置/不可达 → `EmbeddingUnavailable`（新，`answers/ports.py`，或直接 `ProviderRequestError`）→ 503；`search` 的 fingerprint/维度不符 `ValueError` → 409；`resolve` 坏证据 `ValueError` / `ChartQueryError(INVALID_EVIDENCE)` → 409；`ChartQueryError(UNAVAILABLE_EVIDENCE)` → 503；`PIN_CONFLICT` → 409。
- 阶段 1–2 用测试桥 `tests/enterprise_pdf_rag/answers/store_mounted.py::StoreMountedDocument(sources, outputs, publication, processing_id, embedder)`（~40 行，直接组合 `ProcessingRetrieval` + `StoredChartResolver` + `outputs.load_retrieval`），既解耦第 2 项，又是第 2 项实现的参考。

---

## 4. 模块设计

### 4.1 `adapters/hybrid_search.py`（可 import ragspine）

```python
from ragspine.retrieval.lexical.retrieval import bm25_scores, rrf_fuse, tokenize
from ragspine.retrieval.rerank.listwise_rerank import ListwiseJudge, listwise_rerank

@dataclass(frozen=True, slots=True)
class LexicalIndex:
    snapshot_id: str
    member_ids: tuple[str, ...]                 # 与 docs_tokens 同序，按 member_id 升序
    docs_tokens: tuple[tuple[str, ...], ...]
    k1: float = 1.5
    b: float = 0.75
    @property
    def index_id(self) -> str: ...              # sha256(("lexical-bm25-v1", snapshot_id, k1, b, tokenizer_tag))，内容寻址

def build_lexical_index(document: MountedDocument, *, k1=1.5, b=0.75) -> LexicalIndex
    # tokenize(member.text) for member in document.member_texts()；空 token 成员保留（得分 0）

def lexical_rank(index: LexicalIndex, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]
    # bm25_scores(tokenize(query), docs_tokens)；过滤 score<=0；排序键 (-score, member_id)；PinnedRetrievalHit(snapshot_id, member_id, score)

@dataclass(frozen=True, slots=True)
class FusedHit:
    snapshot_id: str
    member_id: str
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None
    vector_score: float | None
    bm25_score: float | None
    def as_hit(self) -> PinnedRetrievalHit: ...

def fuse(vector: Sequence[PinnedRetrievalHit], lexical: Sequence[PinnedRetrievalHit], *, k: float = 60) -> tuple[FusedHit, ...]
    # 校验两路 snapshot_id 一致（否则 ValueError → 409）
    # rrf_fuse([[h.member_id for h in vector], [h.member_id for h in lexical]], k)；排序键 (-fused, member_id)

class HybridSearch:
    def __init__(self, document: MountedDocument, *, channel_limit: int = 20, rrf_k: float = 60,
                 reranker: ListwiseJudge | None = None, index_cache: MutableMapping[str, LexicalIndex] | None = None) -> None
        # index_cache 按 document.retrieval_snapshot_id 取/建 LexicalIndex（进程内 dict；不落盘、不改快照）
    def search(self, query: str, *, top_k: int) -> tuple[FusedHit, ...]
        # vector = document.search(query, limit=channel_limit)   （EmbeddingUnavailable 直接向上抛）
        # lexical = lexical_rank(self._index, query, limit=channel_limit)
        # fused = fuse(vector, lexical, k=rrf_k)
        # if reranker: listwise_rerank(query, [_Candidate(fh, text)], reranker, top_n=top_k)  else fused[:top_k]

class LocalRerankJudge:            # ListwiseJudge 适配 LocalRerankAdapter.rerank(query, documents, *, limit)
    def judge(self, query: str, candidates: list[str]) -> list[int]
```
`_Candidate` 是 `listwise_rerank` 需要的鸭子形状：`chunk.text = member description`, `chunk.sensitivity = "INTERNAL"`（本包无 RESTRICTED 概念，恒定值）。

### 4.2 `processing/context_builder.py`（纯：stdlib + 本包；`check_architecture` 已覆盖 `processing`）

```python
class BlockKind(StrEnum): TEXT = "text"; LIST = "list"; GROUP = "group"; TABLE = "table"; CHART = "chart"

@dataclass(frozen=True, slots=True)
class SpanEvidence:   source_span_id: str; page_index: int; bbox: tuple[float,float,float,float]; text: str
@dataclass(frozen=True, slots=True)
class CellEvidence:   cell_id: str; row: int; col: int; row_span: int; col_span: int; text: str | None; content_state: CellContentState; source_span_ids: tuple[str, ...]
@dataclass(frozen=True, slots=True)
class ChartFieldEvidence:  field_path: str; text: str; value: Decimal | None; value_kind: ValueKind | None; element_ids: tuple[str, ...]
    # field_path ∈ {"title", "period", "points.<pid>.series", "points.<pid>.category", "points.<pid>.unit", "points.<pid>.value"}

@dataclass(frozen=True, slots=True)
class ContextBlock:
    snapshot_id: str; member_id: str; kind: BlockKind; page_index: int
    scope: str                                   # context.scope（资格回执语义范围）
    verification: Verification                   # IR 的 verification
    description_text: str                        # 仅供展示/排序，不作证据
    spans: tuple[SpanEvidence, ...] = ()         # TEXT/LIST/GROUP
    list_items: tuple[tuple[str, ...], ...] = () # LIST.item_groups
    cells: tuple[CellEvidence, ...] = ()         # TABLE
    row_count: int = 0; col_count: int = 0
    grammar: str | None = None                   # CHART
    chart_fields: tuple[ChartFieldEvidence, ...] = ()
    def prompt_text(self) -> str                 # 确定性渲染：
        # TEXT: "[member m..] p.N\n<span_id>: <text>\n..."
        # TABLE: "[member m..] table RxC\n(r,c) cell_id: text" 逐单元格（合并格只出 origin）
        # CHART: "[member m..] chart grammar=bar title=... period=...\npoints.<pid>: series=... category=... unit=... value=<text>|<UNAVAILABLE>"
        # 绝不写入 description_text 里的数字以外的推断；UNAVAILABLE 明示，供拒答

def build_context_block(context: RetrievalContext) -> ContextBlock     # match type(context.ir): TextIR/ListIR/GroupIR/TableIR/ChartIR，其它 kind -> ValueError
def budget_blocks(blocks: Sequence[ContextBlock], *, max_chars: int) -> tuple[ContextBlock, ...]
    # 整块丢弃（保持融合序），不截断块内容（PRD §4.4：不截掉语义前提）；图表块永远整块
```

### 4.3 `answers/`（新顶层纯包；建议加入 `check_architecture.PACKAGES`）

`answers/models.py`
```python
class AnswerStatus(StrEnum): ANSWERED = "answered"; ABSTAINED = "abstained"
class ClaimKind(StrEnum): QUOTE = "quote"; CELL = "cell"; CHART_VALUE = "chart_value"
class AbstainReason(StrEnum):
    NO_RELEVANT_MEMBER = "no_relevant_member"        # 两路皆空 / 预算后无块
    MODEL_DECLINED = "model_declined"                # 模型自报 abstain
    MODEL_OUTPUT_INVALID = "model_output_invalid"    # JSON 不合 schema / 引用不存在的 member/field
    CLAIM_NOT_IN_EVIDENCE = "claim_not_in_evidence"  # 逐字段回读失败且散文依赖该 claim
    NO_VERIFIED_CLAIM = "no_verified_claim"
    # 以下与 figures/chart_qa 两枚举同名，语义一致
    UNQUALIFIED_MEMBER, UNSUPPORTED_GRAMMAR, UNKNOWN_POINT, SERIES_MISMATCH, CATEGORY_MISMATCH,
    PERIOD_MISMATCH, UNIT_MISMATCH, UNSUPPORTED_VALUE_KIND, VALUE_UNAVAILABLE, UNSUPPORTED_PRECISION,
    UNSUPPORTED_OPERATION, INSUFFICIENT_EVIDENCE
def from_refusal(reason: RefusalReason | DisplayedRefusalReason) -> AbstainReason

@dataclass(frozen=True, slots=True)
class AnswerRequest:
    question: str; document_sha256: str | None = None; top_k: int = 6; channel_limit: int = 20
    rerank: bool = False; history: tuple[tuple[str, str], ...] = ()     # history 仅参与提示，不参与检索/校验

@dataclass(frozen=True, slots=True)
class ClaimCitation:
    member_id: str; kind: BlockKind; page_index: int; field_path: str
    evidence_ids: tuple[str, ...]        # span ids / cell_id + source_span_ids / SVG element ids
    bbox: tuple[float, float, float, float] | None
    quote: str                           # 来源原文（逐字）
    chart_citation: FieldCitation | None = None

@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    claim_id: str; kind: ClaimKind; text: str; value: Decimal | None; unit: str | None
    citations: tuple[ClaimCitation, ...]
@dataclass(frozen=True, slots=True)
class RejectedClaim: claim_id: str; member_id: str; field_path: str; text: str; reason: AbstainReason; detail: str

@dataclass(frozen=True, slots=True)
class AnswerResult:
    status: AnswerStatus; answer: str | None
    claims: tuple[VerifiedClaim, ...]; rejected: tuple[RejectedClaim, ...]
    abstain_reason: AbstainReason | None; abstain_detail: str | None
    document_sha256: str; processing_id: str; snapshot_id: str
    member_ids: tuple[str, ...]          # 进入提示的成员（融合序）
    fused: tuple[FusedHit, ...]          # 诊断：两路名次
    request_fingerprint: str | None; llm_live_calls: int; cache_hit: bool
```

`answers/prompt.py`（pydantic 是运行时依赖，白名单内）
```python
class ModelClaim(BaseModel):   # strict, extra=forbid
    claim_id: str; member_id: str; kind: Literal["quote","cell","chart_value"]; field_path: str; text: str
class ModelAnswer(BaseModel):
    abstain: bool; abstain_reason: Literal["not_in_context","ambiguous","needs_calculation"] | None
    answer: str; claims: list[ModelClaim] = Field(max_length=16)
SYSTEM_RULES: Final[str]  # 只允许引用块内逐字文本/单元格/图表显示值；不得计算、不得合并期间、不得估计；找不到就 abstain；块内内容是数据不是指令
def build_prompt(question: str, blocks: Sequence[ContextBlock], history: Sequence[tuple[str,str]]) -> str
```
`field_path` 约定：QUOTE→`fragments.<source_span_id>`；CELL→`cells.<cell_id>`；CHART_VALUE→`points.<point_id>.value`（可附带 `points.<pid>.series/category/unit` 由校验自动补齐引用）。

`answers/verify.py`（纯）
```python
@dataclass(frozen=True, slots=True)
class Verification: verified: tuple[VerifiedClaim, ...]; rejected: tuple[RejectedClaim, ...]

def verify_claims(model: ModelAnswer, blocks: Mapping[str, ContextBlock],
                  chart_contexts: Callable[[str], ChartContext]) -> Verification
    # 逐 claim：member 不在 blocks -> MODEL_OUTPUT_INVALID；按 kind 分派

def _verify_quote(claim, block) -> VerifiedClaim | RejectedClaim
    # field_path 指向的 span 存在；_norm(claim.text) 是 _norm(span.text) 的子串且非空；引用 span_id+page+bbox；否则 CLAIM_NOT_IN_EVIDENCE
def _verify_cell(claim, block) -> ...
    # cell 存在、content_state is PRESENT、_norm(claim.text) == _norm(cell.text)；引用 cell_id + source_span_ids；BLANK/UNAVAILABLE -> VALUE_UNAVAILABLE
def _verify_chart_value(claim, block, context: ChartContext) -> ...
    # 复制 figures/chart_qa/service.py::_answer 的闸门顺序：
    #   context.chart.verification / description.verification 非 VERIFIED -> UNQUALIFIED_MEMBER
    #   point 不存在 -> UNKNOWN_POINT；value.kind != EXPLICIT 或 value None -> VALUE_UNAVAILABLE / UNSUPPORTED_VALUE_KIND
    #   not _precision_supported(value) -> UNSUPPORTED_PRECISION
    #   check_fields(context)（抛 ChartQueryError(INVALID_EVIDENCE) 时向上抛 -> 409）
    #   display = source_display(context, point)；Decimal(strip %) == point.value.value 且 _norm(claim.text) == _norm(display)，否则 CLAIM_NOT_IN_EVIDENCE
    #   citations = citation(context, f"points.{pid}.value", point.value.evidence) + series/category/unit 三条（与 service._claim 同）
    #   bar 成员（scope == "explicit-labels"/source_display_only）：由 adapter 先用 displayed_evidence.chart_context() 投影，并跑 check_displayed_evidence

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*%?")
def prose_grounded(answer: str, verified: Sequence[VerifiedClaim]) -> tuple[bool, tuple[str, ...]]
    # 回答散文中出现的每个数字/百分数 token，规范化后必须等于某个 verified claim 的 text/value；返回 (ok, 越界 tokens)

def decide(model: ModelAnswer, verification: Verification, *, blocks_present: bool) -> tuple[AnswerStatus, AbstainReason | None, str | None]
    # 见 §7 的策略
```

### 4.4 `adapters/answer_service.py`（编排；可 import ragspine）

```python
@dataclass(frozen=True, slots=True)
class AnswerSettings:
    top_k: int = 6; channel_limit: int = 20; rrf_k: float = 60; prompt_budget_chars: int = 18_000
    rerank: Literal["none", "local"] = "none"; prose_gate: Literal["numeric", "numeric+lexical"] = "numeric"
    max_live_calls: int = 1; llm_timeout: float = 45.0

class AnswerService:
    def __init__(self, catalog: DocumentCatalog, llm: JsonCompletionClient, *, settings: AnswerSettings = AnswerSettings(),
                 reranker: ListwiseJudge | None = None, index_cache: MutableMapping[str, LexicalIndex] | None = None) -> None
    def answer(self, request: AnswerRequest) -> AnswerResult
        # 1 document = catalog.mount(request.document_sha256)   （None: 唯一文档，否则 AmbiguousDocument -> 422）
        # 2 fused = HybridSearch(document, ...).search(question, top_k)   -> 空: ABSTAINED NO_RELEVANT_MEMBER（不调 LLM）
        # 3 contexts = {m: document.resolve(h.as_hit())}；blocks = budget_blocks([build_context_block(c)], max_chars)
        # 4 prompt = build_prompt(...)；result = llm.complete_text_json(task="rag-answer-v1", prompt=prompt, response_model=ModelAnswer, max_output_tokens=1024)
        #   JsonCompletionError(code in {call_budget_exhausted, provider_refused, ...}) -> DependencyUnavailable -> 503；invalid_model_json -> ABSTAINED MODEL_OUTPUT_INVALID
        # 5 chart_contexts = lambda m: document.chart_context(hit) / displayed_context 投影（按 contexts[m].scope 选）
        # 6 verification = verify_claims(...)；status = decide(...)；组 AnswerResult
    # 缓存语义：JsonCompletionClient 的 request_fingerprint 含 prompt(含 snapshot/member id) -> 同问同快照重复请求 cache_hit，live call 数为 0（符合"普通迭代不得触发 LLM"）
```

### 4.5 `adapters/http/chat.py`

```python
class RagChatRequest(BoundaryModel):
    model: str; messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False; stream_options: StreamOptions | None = None
    document: str | None = None          # sha256 或 ≥12 位前缀；None -> 从 model 名解析 -> 唯一文档
class ClaimCitationOut(BoundaryModel): member_id, kind, page_index, field_path, evidence_ids: tuple[str,...], bbox, quote, chart_citation: FieldCitationOut | None
class ClaimOut(BoundaryModel): claim_id, kind, text, value: str | None, unit: str | None, citations: tuple[ClaimCitationOut, ...]
class AnswerEnvelope(BoundaryModel):
    status: Literal["answered","abstained"]; abstain_reason: str | None; abstain_detail: str | None
    document_sha256: str; processing_id: str; snapshot_id: str; member_ids: tuple[str,...]
    claims: tuple[ClaimOut, ...]; rejected: tuple[RejectedClaimOut, ...]; llm_live_calls: int; cache_hit: bool
class RagCompletionResponse(CompletionResponse):   # 继承既有形状
    enterprise_pdf_rag: AnswerEnvelope

def render_message(result: AnswerResult) -> str
    # answered: answer + "\n\n引用：\n[1] p.3 span <id>: “…”\n[2] p.5 chart points.p2.value = 8.2% (svg #e17)"
    # abstained: "无法基于已验证证据回答（<reason>）：<detail>"  —— 200

def create_chat_router(catalog: DocumentCatalog, service: AnswerService, *, review: SourceReviewHook | None = None) -> APIRouter
    # GET  /v1/models            -> 目录中每个已发布文档一个 model：id="enterprise-pdf-rag/<sha256[:12]>"（与第 2 项协调，若第 2 项已提供则复用）
    # POST /v1/chat/completions  -> completion(body: RagChatRequest)
    #   last message 非 user / 空 -> 422
    #   source_review_page(content) 命中且 review 可用 -> 原来源审阅路径（保留）
    #   否则 service.answer(AnswerRequest(question, document_sha256=_select(body)))
    #   映射：AmbiguousDocument/未知文档 -> 422/404；EmbeddingUnavailable/ProviderRequestError/DependencyUnavailable/ChartQueryError(UNAVAILABLE_EVIDENCE) -> 503
    #         ValueError(resolve/fuse/pin)/ChartQueryError(INVALID_EVIDENCE|PIN_CONFLICT) -> 409
    #   stream=true -> StreamingResponse(completion_events(response), media_type="text/event-stream")（先算完再切片，复用 openai_demo.completion_events；末帧后附 `data: {"enterprise_pdf_rag": envelope}` 一帧，再 [DONE]）
```
SSE 建议：**不做逐 token 流**（PRD §5 "禁止先流出未校验的模型 token"；ragspine `openai_public.py` 也是同一纪律），直接复用 `completion_events`；ragspine 的 `iter_text_chunks` 无需引入。

---

## 5. 边界模型汇总

- 领域（纯）：`AnswerRequest`、`AnswerResult`、`VerifiedClaim`、`RejectedClaim`、`ClaimCitation`、`AbstainReason`、`AnswerStatus`、`ClaimKind`（`answers/models.py`）；`ContextBlock` 及三种 evidence（`processing/context_builder.py`）；`MemberText`、`MountedDocument`（`answers/ports.py`）；`ModelAnswer`/`ModelClaim`（`answers/prompt.py`，LLM 输出 schema）。
- HTTP（`adapters/http/chat.py`）：`RagChatRequest`、`RagCompletionResponse`、`AnswerEnvelope`、`ClaimOut`、`ClaimCitationOut`、`RejectedClaimOut`；注册到 `scripts/enterprise_pdf_rag/check_schema.py::CONTRACTS["rag-chat-v1"]`，落 `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`。

---

## 6. LLM 客户端选型：`JsonCompletionClient`（enterprise_pdf_rag）

比较：
| | `adapters/json_completion.py::JsonCompletionClient` | `ragspine.agent`（corespine `LLMProvider.chat` + 文本解析） |
|---|---|---|
| 结构化输出 | OpenAI `response_format.json_schema` strict + pydantic `model_validate_json(strict=True)` | 无；自由文本 + 正则/`json.loads` 兜底 |
| 缓存/预算/回放 | 内容寻址缓存、`max_live_calls` 硬预算、跨进程原子 claim、`cache_only` | 无 |
| 测试桩 | `_send_once`/`SmokeSender` monkeypatch 先例 | `MockProvider` |
| 依赖 | 已在本包，`load_llm_config()`（`OPENAI_*`） | 拉入 corespine + ragspine agent 的 FactStore 语义 |
| 缺口 | 强制 `image_png` | 无 claims 结构，anti-fabrication 绑 metric 通路 |

决定：用 `JsonCompletionClient`，**最小改动**：新增 `complete_text_json(*, task, prompt, response_model, max_output_tokens=1024, cache_only=False, allow_failed_retry=True)`，内部与 `complete_json` 共用 `_response_schema/_parse/缓存/预算` 私有路径，仅 user content 为纯文本、fingerprint 加 `"text-only-v1"` 盐；`complete_json` 签名不动。`_send_once` monkeypatch 方式与 `test_generic_publication_e2e.py:94` 一致。

---

## 7. claim 校验失败的处理（推荐："逐条剔除 + 散文数值门"，单次 LLM 调用不重试）

`answers/verify.py::decide`：
1. `model.abstain` → `ABSTAINED(MODEL_DECLINED)`（不因模型拒答而重试）。
2. 逐 claim 校验；**失败的 claim 剔除**并记入 `rejected`（带精确 `AbstainReason`）。
3. `prose_grounded(answer, verified)`：回答散文里任一数字/百分数不在已验证 claim 内 → **整体拒答** `CLAIM_NOT_IN_EVIDENCE`（detail 指出越界 token 与被剔除 claim）。
4. `verified` 为空 → `ABSTAINED(NO_VERIFIED_CLAIM)`；若 rejected 中有图表类原因（如 `VALUE_UNAVAILABLE`/`PERIOD_MISMATCH`），拒答原因取**第一条 rejected 的原因**（比 NO_VERIFIED_CLAIM 更具体，便于验收断言）。
5. 否则 `ANSWERED`，`claims=verified`，`rejected` 原样返回供审计。

理由：PRD §4.9 只有 answered/abstained（partial 需 opt-in）；chart QA gold 要求 100% precision、0 反例逃逸且 all-refusal 不通过——"剔除 + 数值门"保证任何进入散文的数字都有字段级证据（precision），又不因模型多引了一条没用到的证据而全盘拒答（coverage）。备选"任一失败即整体拒答"更简单但会压低 coverage；提供 `AnswerSettings.strict_claims: bool = False` 开关保留该模式。

---

## 8. HTTP 契约要点

- 文档选择优先级：`body.document`（sha256 或前缀，前缀歧义 → 422）> `body.model` 形如 `enterprise-pdf-rag/<sha12>` > 目录唯一文档 > 422 `"document selection required"`。Open WebUI 只能选 `model`，故 `/v1/models` 按目录列出（推荐主路径）。
- 状态码：拒答 200（envelope.status=abstained）；证据损坏/pin/快照不符 409；embedder/LLM/不可变依赖缺失 503；输入不变量 422；未知文档 404。
- 来源审阅路径保留：`source_review_page()` 命中即走旧逻辑（`aia_review.py:154-202` 抽成可注入的 `SourceReviewHook`，或先只在 AIA app 内保留原路由、新路由在通用 app 挂载——阶段 3 时按第 2 项的 app 工厂形态定）。
- 与 ragspine `openai_public.py` 的关系：只借鉴形状（`extra` 忽略 vs 本包 `extra=forbid`：本包沿用 forbid，`temperature` 等参数不接受→422，与既有 `ChatRequest` 一致）。

---

## 9. 独立验收（全离线，`tests/enterprise_pdf_rag/answers/`）

公共桩：
- `store_mounted.py::StoreMountedDocument`（§3 测试桥）；`fake_embedding.py`：确定性 hash→向量 embedder（`EmbeddingPort`，固定 fingerprint/维度），保证向量通道可断言。
- `fake_llm.py::scripted_sender(script: Callable[[str /*prompt*/], ModelAnswer]) -> SmokeSender`：解析 prompt 中的块（`[member <id>]` 行），返回 `{"choices":[{"message":{"content": ModelAnswer.model_dump_json()}, "finish_reason":"stop"}]}`；`monkeypatch.setattr("enterprise_pdf_rag.adapters.json_completion._send_once", ...)` **在构造 client 之前**。
- `gold/*.json`（冻结）：每类 ≥2 正例 + 1 反例，字段：`question, expect_status, expect_reason, expect_member_kind, expect_field_paths[], expect_quote/value`。

| 类别 | 语料 | 正例断言 | 反例（必须拒答） |
|---|---|---|---|
| 普通文本 | `authored_pdf()` → 走 `test_generic_publication_e2e.py` 的 qualify/index/publish（`_text_partition_sender`）得到真实快照 | `status=answered`；claim.kind=quote；`citations[0].evidence_ids == (source_span_id,)`、page 正确、`quote` 是 fragment 逐字子串；BM25 通道单独命中该 span 的 member（`lexical_rank` 确定性断言）；`fused[0].member_id` 稳定 | 问不存在的句子/页：模型被脚本成编造一句 → `abstained` + `CLAIM_NOT_IN_EVIDENCE`；模型自报 abstain → `MODEL_DECLINED` |
| 表格 | `_table_pdf()` → `PdfspineTableAdapter` → TableIR（阶段 4 放行 TABLE 后走同一 publish 流） | claim.kind=cell；`evidence_ids == (cell_id, *source_span_ids)`；`(row,col)` 与 gold 一致 | 问 BLANK/UNAVAILABLE 单元格 → `VALUE_UNAVAILABLE`；脚本返回错误单元格文本 → `CLAIM_NOT_IN_EVIDENCE` |
| 图表 | donut：`published_chart(tmp_path)`；bar：`published_bar_input(tmp_path)`（含 `1H25` 无值） | claim.kind=chart_value；`chart_citation.field_path == "points.<pid>.value"`、`occurrences` 非空；`text == source_display()`；bar 的 `1H24 → 8.2%` | `1H25` → `VALUE_UNAVAILABLE`；问 `bps`/单位不符 → `UNIT_MISMATCH`；跨期差 → 模型给出 `1.3` → `prose_grounded` 失败 → `CLAIM_NOT_IN_EVIDENCE`；篡改 SVG 副本（copy-on-write，禁 hardlink 截断）→ 409 |

另：`adapters/test_hybrid_search.py`（BM25/RRF/tie-break/缓存按 snapshot_id 复用/两路 snapshot 不一致 → ValueError）；`adapters/http/test_chat.py`（200/409/503/422、SSE 帧序、来源审阅仍可用、`/v1/models` 列目录）。评测口径：同报 precision / coverage / refusal；全拒答不通过（gold 正例覆盖必须 100%）。

---

## 10. 分阶段实施清单

**阶段 1 — 检索与上下文（不依赖第 2 项）**
- 新增：`answers/__init__.py`（空）、`answers/ports.py`、`adapters/hybrid_search.py`、`processing/context_builder.py`
- 改：`scripts/enterprise_pdf_rag/check_architecture.py::PACKAGES` + `"enterprise_pdf_rag.answers"`
- 测试：`tests/enterprise_pdf_rag/answers/{store_mounted.py, fake_embedding.py}`、`adapters/test_hybrid_search.py`、`processing/test_context_builder.py`（文字/列表/组/图表块；表格块用手工 `TableIR`）
- 验证：`.venv/bin/python -m pytest tests/enterprise_pdf_rag/answers tests/enterprise_pdf_rag/adapters/test_hybrid_search.py tests/enterprise_pdf_rag/processing/test_context_builder.py -q && .venv/bin/python scripts/enterprise_pdf_rag/check_conformance.py && .venv/bin/python scripts/enterprise_pdf_rag/check_architecture.py`

**阶段 2 — 合成与校验（不依赖第 2 项；用 `StoreMountedDocument` 桥）**
- 新增：`answers/models.py`、`answers/prompt.py`、`answers/verify.py`、`adapters/answer_service.py`、`tests/.../answers/fake_llm.py`、`gold/text-v1.json`、`gold/chart-v1.json`
- 改：`adapters/json_completion.py` + `complete_text_json`
- 测试：`answers/test_verify.py`（纯逻辑，三类 + 数值门）、`adapters/test_answer_service.py`（文字与图表 E2E，正例/反例，`cache_hit` 二次调用 live=0）
- 验证：`.venv/bin/python -m pytest tests/enterprise_pdf_rag/answers tests/enterprise_pdf_rag/adapters/test_answer_service.py -q`
- 这里的 `AnswerService.catalog` 先用一个最小 `DocumentCatalog` Protocol（`mount(sha|None) -> MountedDocument`、`documents() -> tuple[CatalogEntry,...]`），由测试桥实现。

**阶段 3 — HTTP（依赖第 2 项：真实 `DocumentCatalog`/`MountedDocument`、通用 app 工厂）**
- 新增：`adapters/http/chat.py`、`adapters/http/chat_schemas.py`、`docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`
- 改：`adapters/http/app.py`（通用 app 挂 `create_chat_router`）、`scripts/enterprise_pdf_rag/check_schema.py::CONTRACTS`、（可选）`adapters/http/openai_demo.py` 把 `completion_events` 迁到 `adapters/http/sse.py` 并回导
- 第 2 项对接：把 §3 三个新增方法加进其 `MountedDocument`；用 `StoreMountedDocument` 桥对比测试
- 验证：`.venv/bin/python -m pytest tests/enterprise_pdf_rag/adapters/http/test_chat.py -q && .venv/bin/python scripts/enterprise_pdf_rag/check_schema.py`

**阶段 4 — 表格放行 + 全门 + 文档**
- 改：`adapters/processing_retrieval.py::eligibility` 收 `ObjectKind.TABLE`（要求 TableIR `verification is VERIFIED`）；`adapters/literal_qualification.py::validate_literal_member` 返回类型加 `TableIR`；`adapters/draft_publication.py::DraftQualification.qualification_policy` Literal 升 `-v2`（policy 字符串变更 → 新快照 id，旧快照不受影响）
- 测试：`gold/table-v1.json`、`adapters/test_answer_service.py::test_table_*`、表格 publish E2E
- 文档：`docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md`（第 3 项状态）、`testing-and-ingestion.md`（chat 行改为"通用 RAG 回答；拒答 200"）、`src/enterprise_pdf_rag/CLAUDE.md`（布局 + `verified-against` bump）、新 ADR `docs/enterprise-pdf-rag/adr/0011-hybrid-answer-chain.md`
- 验证：`bash scripts/ci.sh`（唯一完成门，从仓库根跑）

---

## 11. 既有文件最小改动清单

| 文件 | 改动 | 阶段 |
|---|---|---|
| `adapters/json_completion.py` | + `complete_text_json`（复用私有路径；`complete_json` 不动） | 2 |
| `adapters/http/app.py` | 通用 app 挂载 chat router（与第 2 项的工厂改动合并） | 3 |
| `adapters/http/openai_demo.py` | 无改动（直接 import `completion_events`）；或迁到 `sse.py` 回导 | 3 |
| `adapters/processing_retrieval.py` | `eligibility` 收 TABLE | 4 |
| `adapters/literal_qualification.py` | 返回类型 + TableIR 分支 | 4 |
| `adapters/draft_publication.py` | policy Literal `-v2` | 4 |
| `scripts/enterprise_pdf_rag/check_architecture.py` | PACKAGES + answers | 1 |
| `scripts/enterprise_pdf_rag/check_schema.py` | CONTRACTS + rag-chat-v1 | 3 |
| `figures/chart_qa/*`、`processing/retrieval.py`、`processing/typed_ir.py`、ragspine 任何文件 | **不改** | — |

---

## 12. 需要拍板的设计点（≤3）

1. **`MountedDocument` Protocol 在第 2 项边界上加三个只读方法**：`member_texts()`（BM25 语料）、`chart_context(hit)`、`displayed_context(hit)`（复用 `StoredChartResolver`/`StoredDisplayResolver`，需 `processing_id`）。**推荐：加。** 否则第 3 项要自己碰 store，违反"只消费两样东西"的边界；三者都是既有函数的薄包装。
2. **claim 校验失败策略**：推荐"逐条剔除 + 散文数值门（越界即整体拒答）"，`strict_claims=False` 默认；备选任一失败即整体拒答。
3. **TABLE 成员放行的范围**：推荐阶段 4 只放行 `TableIR.verification is VERIFIED` 且四个 stage 齐备的表格，qualification policy 升 `-v2`（新索引才生效，已发布 `f59d2308…` 快照不动）；备选把表格验收降级为"手工 TableIR 的纯逻辑测试"，不动资格策略（但那样不算"独立验收表格引用"）。

附带（无需拍板，按推荐执行）：文档选择走 `model` 名 + 可选 `document` 字段；SSE 复用 `completion_events` 不做逐 token；rerank 默认关、`AnswerSettings.rerank="local"` 显式开；`ragspine` 只在 `adapters/` 下 import 三个纯函数 + `listwise_rerank`。
