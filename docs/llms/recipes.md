# recipes —— 可运行最小示例（全部离线 / 零网络 / 不下模型）

> 每个示例都**实跑过**并附**真实输出**。全部走离线确定性路径：`deterministic` embedding（词法散列，
> 非语义）+ `InProcessVectorStore` + `MockProvider` + `FakeQueue`。**不联网、不下模型、无 API key。**
> 安装：`pip install rag-spine`（带连字符）；这些示例的依赖都在精简核内，无需任何 extra。

---

## 0 · 用户资料到答案（推荐高层入口）

```python
from ragspine import RAGSpine

with RAGSpine.local(".ragspine", profile="balanced") as rag:
    report = rag.ingest("./documents")
    result = rag.ask("今年收入为什么增长？")
    print(report.summary)
    print(result.answer, result.sources)
```

等价 CLI：

```bash
ragspine ingest ./documents --workspace .ragspine --profile balanced
ragspine ask --workspace .ragspine --profile balanced "今年收入为什么增长？"
ragspine doctor
ragspine serve --workspace .ragspine --open
```

`serve` 需要 `[service]`，但使用内联队列，不要求 Redis。

---

## 1 · 建 HybridRetriever 检索（纯 BM25 模式）

不注入 embedding 后端 = 纯 BM25 + RRF（精简默认）。

```python
from ragspine.retrieval.chunking.chunking import chunk_document, DocumentMeta
from ragspine.retrieval.lexical.retrieval import HybridRetriever

text = (
    "中国内地FY2024的REVENUE营收同比增长强劲，两位数上升。\n\n"
    "本期运营成本下降，效率与利润率显著改善。\n\n"
    "市场份额稳步扩大，新签客户数量创历史新高。"
)
meta = DocumentMeta(doc_id="ACME_FY2024.pptx", title="业绩回顾", topic="finance", entity="ACME_CN")
chunks = chunk_document(text, meta, max_chars=40, overlap_chars=0)   # 小 max_chars 仅为演示出多块

retriever = HybridRetriever(chunks)                # embedding_backend=None -> 纯 BM25
for h in retriever.search("REVENUE 营收 增长"):
    print(f"{h.chunk.chunk_id}  fused={h.fused_score:.4f}")
```
输出：
```
ACME_FY2024.pptx#c0  fused=0.0164
ACME_FY2024.pptx#c1  fused=0.0161
```
（纯 BM25 模式下，全 query 零命中的块不进结果，故只回两块。）

元数据预过滤（在打分**之前**）：`retriever.search("...", entity="ACME_CN", topic="finance")`。

---

## 2 · 注入向量后端 -> 开双通道（BM25 + 向量 + RRF）

注入 `EmbeddingBackend` 即开向量通道；向量打分委托给可插拔的 `VectorStore` 缝。

```python
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.retrieval.vector.store import InProcessVectorStore

eb = make_embedding_backend("deterministic", dim=64)   # 离线词法散列后端（非语义）
store = InProcessVectorStore()                          # 零依赖确定性内存向量库
retriever = HybridRetriever(chunks, embedding_backend=eb, vector_store=store)

for h in retriever.search("REVENUE 营收 增长"):
    print(f"{h.chunk.chunk_id}  bm25={h.bm25_score:.3f} vec={h.vector_score:.3f} fused={h.fused_score:.4f}")
print("store.count():", store.count())
```
输出：
```
ACME_FY2024.pptx#c0  bm25=6.410 vec=0.566 fused=0.0328
ACME_FY2024.pptx#c1  bm25=0.474 vec=0.206 fused=0.0320
ACME_FY2024.pptx#c2  bm25=0.000 vec=0.354 fused=0.0161
store.count(): 3
```
`vector_store` 留空时检索器会自建 `InProcessVectorStore`；显式传入则由你持有 / 复用。
**诚实声明**：`deterministic` 是非语义后端（与 BM25 高度相关），仅用于离线打通管线。

---

## 3 · NarrativeIndex 端到端：ingest -> retrieve（store-managed 向量）

入库即嵌入落盘（`max_chars` 在构造器上设，控制切块粒度）；retrieve 复用、不重嵌块。

```python
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.lexical.retrieval import NarrativeIndex

cs = ChunkStore(":memory:"); cs.init_schema()
idx = NarrativeIndex(
    cs,
    embedding_backend=make_embedding_backend("deterministic", dim=64),
    vector_store=InProcessVectorStore(),
    max_chars=40, overlap_chars=0,
)
print("ingested:", idx.ingest(text, meta))                 # -> ingested: 3
for x in idx.retrieve("REVENUE 营收 增长", rerank=False):   # 无 judge 时 rerank 无效；显式关掉
    print(f"{x.chunk.chunk_id}  bm25={x.bm25_score:.3f} vec={x.vector_score:.3f} fused={x.fused_score:.4f}")
cs.close()
```
输出（与 recipe 2 逐位一致 —— store-managed 与直连等价）：
```
ingested: 3
ACME_FY2024.pptx#c0  bm25=6.410 vec=0.566 fused=0.0328
ACME_FY2024.pptx#c1  bm25=0.474 vec=0.206 fused=0.0320
ACME_FY2024.pptx#c2  bm25=0.000 vec=0.354 fused=0.0161
```
接线快捷：`build_narrative_retriever(chunk_db, provider=..., embedding_backend=..., vector_store=...)`
（返回 `(retriever, store)`，store 由你 close）。

---

## 4 · 工厂选实现（spec / env），各 extra 怎么开启

```python
from ragspine.retrieval.vector.store import make_vector_store
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend

make_vector_store(None)                 # -> None（检索器用内置默认）
type(make_vector_store("In-Process"))   # -> InProcessVectorStore（大小写/留白不敏感）
type(make_embedding_backend("DETERMINISTIC"))  # -> DeterministicEmbeddingBackend
```
切到真实后端（先装对应 extra，pip 名带连字符）：

| 想要 | 装 | 然后 |
|---|---|---|
| 持久向量库 sqlite-vec | `pip install "rag-spine[vector]"` | `make_vector_store("sqlite_vec", db_path="vec.db")` |
| pgvector / qdrant | `pip install "rag-spine[vector]"` | `make_vector_store("pgvector", ...)` / `make_vector_store("qdrant", ...)` |
| 真实语义 embedding | `pip install "rag-spine[embed]"` | `make_embedding_backend("qwen3")`（cuda/mps/cpu 自适应） |
| OpenAI embedding | `pip install "rag-spine[llm]"` | `make_embedding_backend("openai", api_key=...)` |
| 真实 Claude | `pip install "rag-spine[llm]"` | `AnthropicProvider(model=..., base_url=...)` |
| HTTP + 队列 | `pip install "rag-spine[service]"` | `ragspine.service.api` / `RQQueue` |

也可用 env（缺省 spec 时读）：`RAGSPINE_VECTOR_STORE` / `RAGSPINE_EMBEDDING_BACKEND` /
`RAGSPINE_EMBEDDING_MODEL` / `RAGSPINE_EMBEDDING_DEVICE` / `RAGSPINE_PERSISTENCE_POLICY`。

---

## 5 · 结构化通道：反捏造 + provenance（answer_question）

```python
from ragspine import FactStore, Fact, MockProvider, answer_question

fs = FactStore(":memory:"); fs.init_schema()
fs.upsert_facts([Fact(
    metric_code="REVENUE", entity="ACME_CN", geography="CN", channel="TOTAL",
    period_type="FY", period="2024", value=1320.0, unit="USD_M",
    source_doc_id="ACME_FY2024.pptx", source_locator="slide=6,table=1,row=2,col=3",
)])

print(answer_question("中国内地FY2024的REVENUE是多少", fs, MockProvider()).answer)
print(answer_question("中国内地FY2030的REVENUE是多少", fs, MockProvider()).answer)
fs.close()
```
输出（存在 -> 带来源；不存在 -> 诚实拒答，绝不编数字）：
```
ACME_CN FY2024 REVENUE：1320 USD_M（来源：ACME_FY2024.pptx · slide=6,table=1,row=2,col=3）
查不到：REVENUE / ACME_CN / 2030（渠道 TOTAL）未在事实表中找到。为避免误导，不提供任何推测数字；可尝试调整期间或实体后重问。
```
`AgentResult.sources` 是 `[{'doc': 'ACME_FY2024.pptx', 'locator': 'slide=6,table=1,row=2,col=3'}]`。

---

## 6 · FakeQueue（同步内联，无需 Redis）

```python
from ragspine.service.tasks.task_queue import FakeQueue, JOB_FINISHED

def double_job(payload):                 # 契约：fn(payload: dict) -> dict
    return {"out": payload["n"] * 2}

q = FakeQueue()
jid = q.enqueue(double_job, {"n": 21})   # FakeQueue 立即内联执行
st = q.get(jid)
print(st.status, st.result, st.status == JOB_FINISHED)
```
输出：`finished {'out': 42} True`。换成真实异步：`RQQueue(redis_url)`（需 `[service]` + Redis）。
`JobStatus`（id/status/result/error）直接复用 corespine 中性核。

---

## 7 · ServiceConfig.from_env（corespine load_from_env）

```python
from ragspine.service.config import ServiceConfig

cfg = ServiceConfig.from_env({
    "RAGSPINE_DB_PATH": "data/fact.db",
    "RAGSPINE_PROVIDER": "mock",          # 旧别名，向后兼容 -> provider_type
    "RAGSPINE_EMBEDDING": "deterministic",
    "RAGSPINE_VECTOR_STORE": "in_process",
})
print(cfg.db_path, cfg.provider_type, cfg.embedding, cfg.vector_store)
```
输出：`data/fact.db mock deterministic in_process`。不传 `env` 则读 `os.environ`。

---

## 8 · 隐私 trace 强制拒正文（corespine sink）

```python
from ragspine.common.observability import emit_trace

emit_trace(route="structured", status="found", latency_ms=12)   # OK：只是元数据
print("safe trace OK")
try:
    emit_trace(answer="secret 1320")          # 禁词键 -> 抛 TraceError，绝不落盘
except Exception as e:
    print("rejected:", type(e).__name__)
```
输出：
```
safe trace OK
rejected: TraceError
```
禁词键（大小写不敏感、精确键名）：`answer / value / text / content / prompt / completion / chunk /
chunk_text / body`。隐私 by construction，不靠 reviewer 自觉。

---

## 9 · 统一异常 CorespineError + error_to_dict

ragspine 的 `ProviderError` / `JobError` / `PathNotAllowedError` 都继承 corespine `CorespineError`。

```python
from corespine import CorespineError, error_to_dict
from ragspine.agent.llm_provider import ProviderError

try:
    raise ProviderError("gateway down")
except CorespineError as e:                    # 一个基类抓全家族受控异常
    print("code:", e.code)                     # -> provider.error
    print(error_to_dict(e))
```
输出：
```
code: provider.error
{'type': 'ProviderError', 'code': 'provider.error', 'message': 'gateway down', 'retryable': False, 'context': {}}
```

---

更复杂的端到端（反捏造 + 引用）见 `examples/minimal_rag.py`，或装后跑 `ragspine quickstart` CLI。
