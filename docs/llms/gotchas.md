# gotchas —— 易错点

> 都是真实会绊倒人的坑，全部经代码核对。

## 1 · pip 名 `rag-spine`（带连字符），import 名 `ragspine`（无连字符）

```bash
pip install rag-spine            # ✅ 分发名带连字符
pip install ragspine             # ❌ 装不到（PyPI 上是 rag-spine）
```
```python
import ragspine                  # ✅ import 名无连字符
import rag_spine                 # ❌ 没有这个名字
```
extras 也跟 pip 名走：`pip install "rag-spine[service,vector]"`。

## 2 · 各能力装对应 extra（`pip install "rag-spine[xxx]"`）

精简核（`pip install rag-spine`）零重依赖、可全离线跑（纯 BM25 + `MockProvider` +
`deterministic` embedding + `InProcessVectorStore`）。真实后端经可选 extra 延迟 import：

| 缺什么报错 | 装哪个 |
|---|---|
| 真实 Claude / OpenAI embedding | `[llm]` |
| 真实语义 embedding（sentence-transformers / Qwen3） | `[embed]` |
| 持久向量库（sqlite-vec / pgvector / qdrant） | `[vector]` |
| HTTP 服务 + RQ/Redis 队列 | `[service]` |
| 数字 PDF 表格抽取（docling） | `[pdf]` |
| 扫描 PDF OCR（paddleocr） | `[ocr]`（Linux + NVIDIA GPU） |
| 一键全功能 | `[all]`（自引用聚合，不含 dev） |

缺 extra 时**不**会在 `import ragspine` 时炸 —— 只有真正用到那条链（如实例化 `OpenAIEmbeddingBackend`）
才延迟 import，并由 corespine `lazy_extra_import` 翻译成可执行的 `pip install ragspine[xxx]` 友好提示。

## 3 · GPU 自适应，无需选版

`[embed]` / `[ocr]` 的 torch / transformers / paddleocr 装的是**自适应版**：运行时探测到 CUDA 即用
GPU，否则回落 CPU；`SentenceTransformerEmbeddingBackend` 经 `_select_torch_device` 自动选 `cuda → mps →
cpu`，也可用 `RAGSPINE_EMBEDDING_DEVICE`（只接受 `cuda`/`mps`/`cpu`）或构造器 `device=` 覆盖。
**别**为 CPU/GPU 装不同包。注意 `[ocr]` 的 paddleocr 真实运行需 Linux + NVIDIA GPU。

## 4 · 向量后端别名（spec 大小写 / 留白 / 连字符不敏感）

`make_embedding_backend`：
- `qwen3` / `sentence-transformers` / `st` —— 都指向同一个 `SentenceTransformerEmbeddingBackend`。
- `None` / `"none"` —— 返回 `None` = 纯 BM25（不是报错！）。
- `deterministic` / `openai` —— 各自后端。

`make_vector_store`：
- `in_process` / `in-process` / `memory` —— 都是 `InProcessVectorStore`。
- `None` / `"none"` —— 返回 `None`（检索器自建内置默认；与显式 `InProcessVectorStore()` 在结果上等价）。
- `sqlite_vec` / `sqlite-vec`、`pgvector` / `pg_vector`、`qdrant` —— adapter（需 `[vector]`）。

## 5 · `make_vector_store` 用 ragspine 自己的 entry-point 发现，**不是** corespine.Registry

容易搞混：`make_embedding_backend` **是** 薄封装 corespine `Registry`；但 `make_vector_store`
**没有**折进 corespine.Registry —— 它是 ragspine 自己的内置 loader 表 + entry-point 自动发现
（group `ragspine.vector_stores`）。第三方包在该 group 下注册一行即可被 `make_vector_store("名字")`
选中，核心零 PR。别误以为两个工厂同源。

## 6 · 消费 corespine 的两条硬约束

**(a) 隐私 trace 强制拒正文。** `emit_trace(**fields)` 内嵌 corespine `InProcessPrivacyTraceSink`：
载荷若含禁词键 —— `answer` / `value` / `text` / `content` / `prompt` / `completion` / `chunk` /
`chunk_text` / `body`（大小写不敏感、精确键名匹配）—— **直接抛 `TraceError`、绝不落盘**。
只传非敏感元数据（route / 状态计数 / 分数 / 耗时 / token 用量）。这是机制，不是约定。

```python
emit_trace(route="narrative", n_chunks=5, latency_ms=42)   # ✅
emit_trace(answer="...")                                    # ❌ raise TraceError
emit_trace(chunk_text="...")                                # ❌ raise TraceError
```

**(b) 统一异常基类 `CorespineError`。** ragspine 的受控异常都继承它，带稳定 `code`：

| 异常 | code | 何时 |
|---|---|---|
| `ProviderError`（agent.llm_provider） | `provider.error` | LLM/embedding SDK 网络/API 失败 |
| `JobError`（service.tasks.task_queue） | `job.error` | job 执行受控失败（带 `stage` / `retryable`） |
| `PathNotAllowedError`（service.config） | `config.path_not_allowed` | ingestion 路径越界 / 坏后缀 |

```python
from corespine import CorespineError, error_to_dict
try:
    ...
except CorespineError as e:        # 一个基类抓全家族受控异常
    log.warning(error_to_dict(e))  # {'type','code','message','retryable','context'}
```
注意：`ProviderError` 只包裹 SDK 网络/API 异常；程序错误（KeyError/TypeError）照常上抛，不被兜底掩盖。

## 7 · 顶层只 curated 暴露 4 个名字；其余靠惰性子模块点到底

`from ragspine import X` 只有 `FactStore` / `Fact` / `MockProvider` / `answer_question`。其它都走子模块
全路径：`from ragspine.retrieval.lexical.retrieval import HybridRetriever`。深层、按领域分组的布局
——**先按文件夹定位职责，再读文件名**。拼错子模块名抛 `AttributeError`；子模块存在但缺第三方可选依赖
（如 service 缺 fastapi）则**原样大声上抛**，绝不静默吞。

## 8 · 纯 BM25 默认无语义信号；`deterministic` 也是非语义

不注入 embedding 后端 = 纯 BM25 + RRF（精简默认，无语义召回）。`deterministic` embedding 是
**词法散列**后端（与 BM25 高度相关的非语义信号），用于离线 / 测试 / 打通管线 —— **绝不**代表真语义
增益。真语义需 `[embed]` 的真实模型（如 Qwen3，待 GPU）。文档里凡 `deterministic` 输出都只为演示管线连通。

## 9 · `init_schema()` 必须先调；store 由调用方 close

`FactStore` / `ChunkStore` 构造后**必须** `init_schema()` 才能用。`build_narrative_retriever` /
`open_*` 返回的 store 由调用方负责 `close()`（或用 `service.config` 的 contextmanager 版
`open_fact_store` / `open_narrative_retriever`）。

## 10 · `rerank=True` 但没给 judge 时静默不精排

`NarrativeIndex.retrieve(..., rerank=True)` 在 `judge is None` 时直接返回 RRF 序（不报错）。要真正
listwise 精排得注入 `judge`（如 `ProviderListwiseJudge(provider)`），否则 `rerank` 标志无效。
