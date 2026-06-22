# overview —— ragspine 是什么、解决什么、核心概念

> 本目录（`docs/llms/`）为 AI/LLM 消费者写。所有 API、签名、示例输出均经真实 `import ragspine`
> 运行核对。pip 名 **`rag-spine`**（带连字符），import 名 **`ragspine`**（无连字符）。

## 是什么

ragspine 是一个**在普通 Python 里装配**的后端 RAG 引擎 —— 不是要你提交给它的框架。它在
hand-rolled glue 与重型编排平台（Dify / LangGraph，各自带运行时、图 DSL、UI、锁定）之间走中间路线：
一个连贯的、batteries-included 的可组合零件库（检索、agent 编排、文档抽取、评测、HTTP 服务层），
靠普通函数和带类型的 `Protocol` 接起来。

为一个苛刻场景而建（对财报 / 经营报告做管理层洞察问答），并把那份严格度作为**代码级、可执行的不变量**出货：

- **绝不捏造（anti-fabrication）。** 数据不在时，编排层**确定性**地拒答（"查不到"），无论模型说什么。
  反捏造活在控制流里，不在你寄望模型遵守的 prompt 里。
- **永远引用（provenance）。** 每条答案携带来源血缘（文档 + locator）。
- **双通道、一个路由。** 确定性**结构化/数值**通道（事实表 + function-calling）回答"数字是多少"，
  **叙事 RAG** 通道（混合检索 + 精排）回答"为什么 / 发生了什么"。agent 给每个问题路由 —— 或把复合问题
  拆开两路都跑。
- **一切可插拔。** LLM provider、embedding 后端、reranker、OCR、retriever、vector store、task queue ——
  都是注入在边缘的带类型 `Protocol`。核心 import 零 SDK，可全离线跑确定性 `MockProvider`。

## 核心概念

### 双通道（dual-channel）

| 通道 | 入口 | 回答 | 机制 |
|---|---|---|---|
| 结构化 / 数值 | `answer_question` 路由到 `query_metric` 工具 | "数字是多少" | 每个数字都在 `fact_metric` 表里，带全血缘；function-calling 返回 `found` / `not_found` / `unrecognized`，agent 绝不让模型编数字 |
| 叙事 RAG | `NarrativeIndex` / `HybridRetriever` | "为什么 / 发生了什么" | chunking -> 混合检索（BM25 + 注入向量 + RRF）-> 可选 LLM listwise 精排 -> 带引用合成 |

agent 解析意图（metric / entity / period / channel）后路由：`structured` / `narrative` / `composite`（两路都跑、对比、合并）。

### 混合检索：BM25 + 向量 + RRF

`HybridRetriever`（`ragspine.retrieval.lexical.retrieval`）：

1. **元数据预过滤** —— 在任何打分 / embedding **之前**按 topic / entity / geography / period / language 收窄候选。
2. **BM25**（纯 Python Okapi，`k1=1.5, b=0.75`）—— 中英混排分词（ASCII 按词、CJK unigram+bigram）。
3. **向量通道**（可选）—— 注入 `EmbeddingBackend` 后开启；向量**打分**委托给可插拔的 `VectorStore` 缝
   （默认零依赖内存 `InProcessVectorStore`）。不注入 embedding 后端即纯 BM25 模式。
4. **multi-query 改写** —— 默认 `GlossaryQueryRewriter` 用 glossary 同义词做确定性规则改写（零 LLM）。
5. **RRF 融合**（`k=60`，rank 从 1 起，`score += 1/(k+rank)`）-> top-k，平分按 chunk_id 升序（确定性）。

### chunking

`chunk_document(text, DocumentMeta(...))`（`ragspine.retrieval.chunking.chunking`）把文本切成段落粒度的
`Chunk`，确定性 `chunk_id`（`{doc_id}#c{seq}`）。`NarrativeIndex.ingest` 走切块 + 幂等入库 +（有向量后端时）
入库即嵌入落盘。

### provenance / 反捏造

- 结构化：`Fact` 带 `source_doc_id` + `source_locator`；`answer_question` 的 `AgentResult.sources` 是
  `[{"doc": ..., "locator": ...}]`。`not_found` 由编排层确定性生成 —— 模型想编也被拦改写。
- 叙事：每个 snippet 带 `doc_id` / `source_locator`；`RESTRICTED` 内容在两道出口（retrieval/link 与
  retrieval/rerank）被剔除，绝不进 prompt。

### VectorStore 缝（可插拔向量索引）

`VectorStore` Protocol（`ragspine.retrieval.vector.store`）只担一件事：**存向量 + 回答一次带 `where`
过滤的 top-k 相似度查询**。它**不**管 BM25 / RRF / rerank。三项不变量被实现真正保证（非注释）：
**provenance**（id + metadata 原样存回）、**isolation**（`where` 过滤在打分前施加）、**determinism**
（候选按 id 升序、结果按 `(-score, id)` 排序，跨调用逐位一致）。

- 默认：`InProcessVectorStore`（零三方依赖、确定性、brute-force cosine）。
- 选型：`make_vector_store(spec)` / 环境变量 `RAGSPINE_VECTOR_STORE`。
- 内置 adapter（behind `[vector]`）：`sqlite_vec`（嵌入式）、`pgvector`（Postgres，BSD pg8000 驱动）、
  `qdrant`（HNSW，local 模式）。
- 第三方包可经 entry-point group `ragspine.vector_stores` 注册一个后端按名选中 —— **核心零 PR**。
  （注意：vector store 工厂用的是 ragspine 自己的 entry-point 发现，**没有**折进 corespine 的 Registry。）

## 与 corespine 的关系

ragspine 依赖 **corespine**（Spine 家族「薄」共享核，ADR 0001 D3/D5）—— 一组 domain-neutral 的底层原语。
ragspine 在 6 条「缝」上接入它，但 corespine **不**反向依赖 ragspine、也不含任何 RAG 概念：

| 缝 | ragspine 怎么用 corespine | 位置 |
|---|---|---|
| **config** | `ServiceConfig.from_env` 用 corespine `load_from_env` + `env_key`（`PREFIX_FIELDNAME` -> frozen dataclass） | `service/config.py` |
| **registry** | `make_embedding_backend` 用 corespine `Registry` + `lazy_extra_import`（名字->工厂 + spec 归一 + 缺 extra 友好提示） | `retrieval/vector/embedding_backends.py` |
| **trace** | `emit_trace` 内嵌 corespine `InProcessPrivacyTraceSink`，载荷含禁词键（answer/value/text/...）**直接抛 `TraceError`、绝不落盘** | `common/observability.py` |
| **queue** | `task_queue` 复用 corespine `JobStatus`（id/status/result/error）+ `error_to_dict`；`TaskQueue` 协议扩展 corespine `TaskQueue` | `service/tasks/task_queue.py` |
| **errors** | `ProviderError` / `JobError` / `PathNotAllowedError` 都继承 corespine `CorespineError`（稳定 `code`：`provider.error` / `job.error` / `config.path_not_allowed`） | 各处 |
| **conformance** | `VectorStore` conformance 用 corespine `ConformanceSuite` × `InvariantPack`（实现 × 不变量 笛卡尔积） | `tests/conformance/` |

本地开发 `pyproject.toml` 经 `[tool.uv.sources]` 把 corespine 指向同家族目录源码（可编辑）；发布层面二者各自独立发版。

## 架构主线（请求流）

```
question
  → intent parse (metric / entity / period / channel)
  → clarification gate ──(歧义)→ 反问  ──(越权/竞品实体)→ 拒答
  → FAQ short-circuit (service 边缘) ──(SME 命中)→ 缓存答案 + provenance
  → route:
       structured → 对事实表 function-calling → found / not_found / unrecognized
       narrative  → 混合检索 → listwise 精排 → 带引用合成
       composite  → 两路都跑、对比、合并
  → answer + sources   (反捏造守卫：无事实则重写为"查不到")
```

包按领域分组（先按文件夹定位职责，再读文件名）：`common/` `extraction/` `ingestion/` `storage/`
`retrieval/`（`chunking/` `lexical/` `vector/` `rerank/` `link/`）`agent/` `eval/` `pipeline/` `service/` `cli/`。

下一步：API 签名见 [api.md](api.md)，可运行示例见 [recipes.md](recipes.md)，坑见 [gotchas.md](gotchas.md)。
