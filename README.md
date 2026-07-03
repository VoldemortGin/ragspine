# RAGSpine

> The framework-free backbone for backend RAG. Deterministic dual-channel retrieval
> and agent orchestration, with **anti-fabrication** and **source provenance** built in —
> no Dify, no LangGraph, no DSL. Just composable Python.

> **🤖 给 AI / LLM:** 用本库前先读 [`llms.txt`](llms.txt)（精简索引）与 [`docs/llms/`](docs/llms/)（完整 API / recipes / 陷阱）；`pip install` 后这些文档随包位于 `site-packages/ragspine/_llms/`。

[![PyPI](https://img.shields.io/pypi/v/rag-spine.svg)](https://pypi.org/project/rag-spine/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-~1955%20passing-brightgreen)
[![Docs](https://img.shields.io/badge/docs-rag--spine.org-2dd4bf)](https://rag-spine.org)

---

## What is RAGSpine?

RAGSpine is a backend RAG engine you **assemble in plain Python** — not a framework you
submit to. Most stacks force a choice between hand-rolled glue and heavyweight
orchestration platforms (Dify, LangGraph) that drag in their own runtime, graph DSL, UI,
and lock-in. RAGSpine is the middle path: a coherent, batteries-included library of
composable parts — retrieval, agent orchestration, document extraction, evaluation, and an
HTTP service layer — wired together by ordinary functions and typed `Protocol`s.

It was built for a demanding use case (executive insight Q&A over financial and operational
reports) and ships that rigor as **first-class, code-enforced invariants**:

- **Never fabricate.** When the data isn't there, the orchestrator deterministically
  refuses ("not found") regardless of what the LLM says. Anti-fabrication lives in the
  control flow, not in a prompt you hope the model obeys.
- **Always cite.** Every answer carries source lineage (document + locator).
- **Two channels, one router.** A deterministic **structured/numeric** channel (a fact
  table + function-calling) answers *"what's the number,"* and a **narrative RAG** channel
  (hybrid retrieval + rerank) answers *"why / what happened."* An agent routes each
  question — or splits a composite one and runs both.
- **Everything is pluggable.** LLM provider, embedding backend, reranker, OCR, retriever,
  task queue — all are typed `Protocol`s injected at the edges. The core imports zero SDKs
  and runs fully offline with a deterministic `MockProvider`.

## Why RAGSpine

| | |
|---|---|
| **No framework lock-in** | Pure Python; bring your own everything. Drop into any backend. |
| **Dual-channel** | Deterministic numbers + narrative RAG, unified by an agent router. |
| **Anti-fabrication + provenance** | Enforced invariants, not prompt suggestions. |
| **Office-document extraction** | xlsx / pptx / pdf → *structured facts*, style- and color-aware — not just text splitting. |
| **Hybrid retrieval** | CJK-aware BM25 + injectable vector channel + RRF fusion + optional LLM listwise rerank. |
| **FAQ short-circuit** | SME-vetted answers bypass the LLM, behind conservative exclusion guards. |
| **Built-in evaluation** | Four-gate metrics (numeric accuracy / citation validity / refusal / clarification) + baseline regression gating. |
| **Async ingestion** | FastAPI service + RQ/Redis job queue, worker-owned resources. |
| **Privacy-aware observability** | Traces carry codes/counts/timings only — never answer, fact, or chunk text. |

## RAG capabilities

RAGSpine covers the mainstream RAG technique surface — benchmarked stage-by-stage against
LlamaIndex · LangChain/LangGraph · Haystack · RAGFlow · Weaviate · Vespa · Jina — organized
by retrieval-pipeline layer. **The default loop is offline and deterministic; every
model-bearing or LLM technique is opt-in and leaves the default byte-identical** (ADR 0001/0005).

| Pipeline layer | Techniques (industry names) | Default / opt-in |
|---|---|---|
| **Indexing & chunking** | paragraph-greedy · layout-aware · parent-child / small-to-big · sentence-window · semantic (embedding-boundary) · RAPTOR recursive-summary tree · contextual retrieval (deterministic context headers) | paragraph-greedy default; rest opt-in (`Chunker` seam) |
| **Retrieval representation** | hybrid **BM25 + dense (ONNX MiniLM) → RRF** · ColBERT late-interaction (multi-vector MaxSim) · SPLADE learned-sparse | hybrid default with `[embed-onnx]` (else pure BM25); ColBERT/SPLADE opt-in *(as rerankers; multi-vector/sparse retrieval index = follow-up)* |
| **Query transformation** | controlled-vocab synonym multi-query · LLM decomposition · HyDE · RAG-Fusion · step-back · Adaptive-RAG (complexity routing) | synonym multi-query default (deterministic); LLM transforms opt-in |
| **Post-retrieval & rerank** | LLM listwise rerank · **cross-encoder rerank** · MMR diversity de-dup · lost-in-the-middle reorder · context compression | opt-in (`ListwiseJudge` + `NodePostprocessor` seams) |
| **Generation & agentic** | dual-channel router · anti-fabrication guard · CRAG / self-RAG corrective retrieval · multi-turn conversational memory | router + guard default; corrective/memory opt-in |
| **Graph** | deterministic structured relation graph (subsidiary roll-up · peer comparison · derivation trace · doc co-occurrence) + `GraphStore` seam · narrative GraphRAG | in-process graph default; narrative GraphRAG opt-in *(skeleton)* |
| **Multimodal** | ColPali page-as-image visual retrieval (late interaction, no OCR→text) | opt-in, GPU *(seam + orchestration; real GPU end-to-end = follow-up)* |
| **Evaluation** | four-gate (numeric accuracy · citation validity · refusal · clarification) + **faithfulness/groundedness** + free-text answer-accuracy, baseline-ratcheted | offline deterministic default *(ONNX-NLI / RAGAS = follow-up)* |

**The moat competitors don't have:** enforced **anti-fabrication** + **source provenance**, the
spine family's **offline OCR & strong-table extraction** (pdf→ppt→doc, `pdfspine`→`ocrspine`,
scanned PDFs actually OCR'd on CPU), and an **offline deterministic charter** — the whole
pipeline runs without a network, a GPU, or a framework.

## Architecture

A deep, domain-grouped package layout — find the file by folder before you read a name.

```
src/ragspine/
├── common/         cross-cutting: company profile, sensitivity, glossary, observability
├── extraction/     documents → a frozen StyledGrid intermediate representation (IR)
│   ├── extractors/   xlsx / pptx / pdf (digital + scanned/OCR), style- & color-aware
│   ├── routing/      per-page PDF triage (digital vs scanned vs export)
│   ├── color/        controlled color-semantics registry
│   └── verification/ dual-channel cross-check → review queue
├── ingestion/      IR/text → stores
│   ├── structured/   fact ingestion + batch manifest ledger (idempotent)
│   ├── narrative/    document chunk ingestion + extraction
│   └── review/       human review-queue state machine (SME)
├── storage/        fact store (numeric) + chunk store (narrative), sqlite, full lineage
├── retrieval/      narrative RAG
│   ├── chunking/     paragraph-granular chunker + versioned chunk store
│   ├── lexical/      Okapi BM25 (CJK uni+bigram) + RRF fusion
│   ├── vector/       injectable embedding backends (default: none = pure BM25)
│   ├── rerank/       LLM listwise reranker (RRF-fallback)
│   └── link/         adapter wiring retrieval into the agent (strips RESTRICTED at exit)
├── agent/          intent parsing, clarification gateway, tool-use loop, llm provider
├── eval/           QA + extraction evaluation harnesses with baseline gates
└── service/        FastAPI app, RQ task queue, ingestion jobs, FAQ short-circuit cache
```

**Request flow**

```
question
  → intent parse (metric / entity / period / channel)
  → clarification gate ──(ambiguous)→ ask  ──(out-of-scope entity)→ refuse
  → FAQ short-circuit (service edge) ──(vetted hit)→ cached answer + provenance
  → route:
       structured → function-calling over the fact store → found / not_found / unrecognized
       narrative  → hybrid retrieve → listwise rerank → synthesize with citations
       composite  → run both, compare, merge
  → answer + sources   (anti-fabrication guard rewrites to "not found" if no fact)
```

## Install

```bash
pip install rag-spine      # distribution name is hyphenated; the import is:  import ragspine
```

Optional extras:

| Extra | Pulls in | For |
|---|---|---|
| `[service]` | fastapi, uvicorn, rq, redis, httpx | the HTTP + async-queue layer |
| `[pdf]` | pdfspine | digital-PDF table extraction (pure-Rust, offline); `[pdf-docling]` for the docling fallback |
| `[ocr]` | paddleocr | scanned-PDF OCR VLM (Linux + NVIDIA GPU) — the family OCR (`pdfspine`→`ocrspine`) needs no extra |
| `[doc]` / `[ppt]` | docspine / pptspine | family `.docx` / `.pptx` extraction (strong tables, offline) |
| `[llm]` | anthropic, openai | real LLM providers (lazy-imported) |
| `[embed-onnx]` | fastembed | **default semantic embedding** — ONNX MiniLM, CPU, dense-on hybrid |
| `[embed]` | sentence-transformers | heavier embedding models for the vector channel |
| `[rerank]` / `[colbert]` / `[splade]` | fastembed | local cross-encoder / ColBERT late-interaction / SPLADE learned-sparse rerankers |
| `[colpali]` | fastembed | ColPali page-as-image visual retrieval (GPU) |
| `[vector]` | sqlite-vec, pg8000, qdrant-client | persistent `VectorStore` backends: sqlite-vec (embedded) + pgvector (Postgres, BSD driver) + qdrant |
| `[graph]` | networkx | `GraphStore` adapter for the relation graph |
| `[all]` | all of the above | one-shot install of every optional backend |
| `[dev]` | pytest, reportlab, markdown | tests + fixture generation |

Install optional backends on demand — the lean default (`pip install rag-spine`) runs offline on
pure BM25 with zero heavy deps; the quality-depth backends are all opt-in.

**From source**

```bash
git clone https://github.com/VoldemortGin/ragspine.git && cd ragspine
uv venv .venv
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev,service,vector]"
```

## Quickstart

**0. Fastest smoke test — the installed `ragspine` CLI, offline, no API key:**

```bash
ragspine quickstart    # one FOUND answer (with provenance) + one honest "not found" — proves anti-fabrication in seconds
```

**1. End-to-end demo on synthetic data — offline, no API key:**

```bash
.venv/bin/python scripts/run_demo.py        # → ALL CHECKS PASSED
```

**2. Ask a question (offline deterministic `MockProvider`):**

> Needs the fact store from step 1 — `scripts/run_demo.py` populates the gitignored
> `data/fact_metric.db`. `ragspine ask` errors honestly if the db is missing; it never
> silently invents an empty one and returns a false "not found".

```bash
ragspine ask --db data/fact_metric.db "中国内地FY2024的REVENUE是多少"
# → ACME_CN FY2024 REVENUE 为 1320 USD_M（来源：ACME_FY2024_Review.pptx · slide=2,table=1,row=REVENUE,col=FY2024）
```

Ask for something the data doesn't have and you get an honest refusal, never a guess:

```bash
ragspine ask --db data/fact_metric.db "中国内地FY2025的REVENUE是多少"
# → 查不到：REVENUE / ACME_CN / 2025 …未在事实表中找到。为避免误导，不提供任何推测数字。
```

**3. Python API:**

```python
from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.storage.fact_store import FactStore

store = FactStore("data/fact_metric.db"); store.init_schema()
result = answer_question("中国内地FY2024的REVENUE是多少", store, MockProvider())
print(result.answer)     # deterministic value, or an honest "not found"
print(result.sources)    # [{'doc': ..., 'locator': ...}]
```

**4. HTTP service + async ingestion:**

```bash
# API
RAGSPINE_DB_PATH=data/fact_metric.db .venv/bin/python scripts/run_server.py --port 8000
curl -s localhost:8000/v1/ask -H 'content-type: application/json' \
     -d '{"question":"中国内地FY2024的REVENUE是多少"}'

# worker (needs Redis) — ingestion jobs run out-of-process
RAGSPINE_REDIS_URL=redis://localhost:6379/0 .venv/bin/python scripts/run_worker.py
```

Endpoints: `GET /healthz`, `GET /readyz`, `POST /v1/ask`,
`POST /v1/ingest/structured/jobs`, `POST /v1/ingest/narrative/jobs`, `GET /v1/jobs/{id}`.

## Core concepts

- **Structured channel** — every number lives in a `fact_metric` table with full lineage
  (`source_doc_id` + `source_locator`). A `glossary` normalizes ZH/EN/abbrev synonyms to
  controlled metric/entity/period codes (returns `None` rather than guessing). A
  function-calling `query_metric` tool returns `found` / `not_found` / `unrecognized` — and
  the agent **never** lets the model invent a number.
- **Narrative channel** — `chunking` → hybrid `retrieval` (BM25 + injectable vector + RRF
  k=60 + glossary multi-query) → optional LLM `listwise_rerank` → synthesis with citations.
  `RESTRICTED`-tier content is filtered at **two** exits before it can reach a prompt.
- **Agent** — four-slot intent parse → clarification gateway (answer-first, expose
  assumptions, one-click narrow) → route → anti-fabrication guard.
- **Ingestion** — extractors emit a frozen `StyledGrid` IR; structured & narrative
  ingestion are hash-idempotent; low-confidence/conflicting items go to a human review
  queue (distinct from the async job queue).
- **FAQ cache** — SME-vetted Q→A short-circuit with conservative exclusions: structured
  numeric questions, competitor/external entities, real-time queries, expired, disabled,
  and `RESTRICTED` items never short-circuit.
- **Config** — `ServiceConfig` (env-driven, `RAGSPINE_*`) + `CompanyProfile`
  (`config/company.example.toml` → copy to `config/company.toml`).

## Extension points (just implement a Protocol)

`LLMProvider` · `EmbeddingBackend` · `VectorStore` · `PersistencePolicy` · `ListwiseJudge` ·
`OcrBackend` · `Extractor` · `Chunker` · `NarrativeRetriever` · `NodePostprocessor` ·
`QueryDecomposer` / query-transform · `RelevanceGrader` · `EntailmentJudge` · `GraphStore` ·
`VisualEmbedder` · `TaskQueue` — implement and inject. The core depends on the
abstraction, never the SDK, so adding a provider / vector store / reranker / OCR engine touches one
new file. The quality-depth seams are config-selectable by a `make_*` factory + `RAGSPINE_*` env,
mirroring `make_vector_store`: reranker (cross-encoder / ColBERT / SPLADE via `make_reranker`),
postprocessor (MMR / lost-in-the-middle / compression via `make_postprocessor`), query transform
(HyDE / RAG-Fusion / step-back / adaptive via `make_query_transform`), chunker (layout /
parent-child / sentence-window / semantic via `make_chunker`), `make_graph_store`, and
`make_visual_embedder` — each with an offline default and its own provenance/isolation conformance
pack. `VectorStore` ships a conformance kit (`tests/conformance/`) that binds provenance /
isolation / determinism for *any* implementation; select one by config (`make_vector_store` /
`RAGSPINE_VECTOR_STORE`), or let a third-party package register a backend by name via the
`ragspine.vector_stores` entry-point group (no core PR). With a persistent store, `NarrativeIndex` embeds-and-persists at ingest
(so a fresh process re-uses vectors, no re-embedding); the swappable `PersistencePolicy` gates what
is written at rest — its default **never persists a `RESTRICTED` chunk's vector**.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `RAGSPINE_DB_PATH` | `data/fact_metric.db` | fact (numeric) store |
| `RAGSPINE_CHUNK_DB_PATH` | _(unset → narrative degrades honestly)_ | narrative chunk store |
| `RAGSPINE_PROVIDER` | `mock` | `mock` \| `anthropic` |
| `RAGSPINE_MODEL` / `RAGSPINE_BASE_URL` | — | model + enterprise-gateway override |
| `RAGSPINE_REDIS_URL` | `redis://localhost:6379/0` | RQ job queue |
| `RAGSPINE_FAQ_SOURCE` | — | path to the FAQ JSON |
| `RAGSPINE_ALLOWED_UPLOAD_ROOT` | — | ingestion path allowlist (rejects traversal) |

## Testing

```bash
.venv/bin/python -m pytest tests/ -q -m "not network and not gpu"   # ~1955 passed (the CI default)
```

The project is **test-driven**: tests are the spec. The `gpu` marker gates real-OCR / ColPali
integration tests to a Linux + NVIDIA GPU box; the `network` marker gates the real-model
(fastembed/HF "first-pull-then-offline") assertions — both are excluded by `make ci`. The
**`pgvector` conformance** skips unless `RAGSPINE_PG_URL` points at a Postgres with the pgvector
extension (the adapter is conformance-bound, just not in the default server-less CI). The
**Qdrant** conformance runs unconditionally (local mode is purely in-process, no server).
Everything else runs anywhere.

## Continuous integration (local)

CI runs **on your machine**, not on GitHub Actions. `scripts/ci.sh` is the gate (full test
suite, gpu-excluded, + demo smoke), and a pre-push hook enforces it so red code never gets
pushed:

```bash
scripts/ci.sh                        # run the gate manually
git config core.hooksPath .githooks  # enable the pre-push gate (once per clone)
```

`.github/workflows/ci.yml` is included but **dormant** — manual-trigger only — so it consumes
zero Actions minutes. Uncomment its `push:` / `pull_request:` triggers to enable server-side
CI; it runs the exact same `scripts/ci.sh`. Lint / type-check (`scripts/lint.sh`, ruff + mypy)
is opt-in and informational for now (the inherited code predates linting).

## Demo data

The bundled demo uses a **fictional** company (ACME), synthetic figures, and a fictional
competitor set — all generated by `scripts/make_*.py` (regenerable, deterministic). The
version-controlled evaluation sets live under `data/golden/`. Nothing here is real-world data.

## Status & roadmap

**Current release: 0.8.0.** It completes the twelve-workstream **quality-depth** program
([`docs/prd-quality-depth.md`](docs/prd-quality-depth.md)), benchmarked stage-by-stage against the
mainstream RAG stacks in two batches — every new capability **opt-in, the default loop byte-identical**:

- **This release (W8–W12, the second competitor batch):** post-retrieval postprocessor chain
  (MMR de-dup · lost-in-the-middle · context compression), LLM query transforms (HyDE · RAG-Fusion ·
  step-back · Adaptive-RAG), RAPTOR recursive-summary tree + sentence-window / semantic chunking,
  ColBERT late-interaction + SPLADE learned-sparse rerankers, and ColPali page-as-image visual retrieval.
- **0.7.0 (W1–W7, the first batch):** true semantic ONNX embedding default + dense-on hybrid, local
  cross-encoder rerank, family OCR / `.docx` / `.pptx` / rich-table extraction, contextual + parent-child
  chunking, the faithfulness/groundedness eval gate, agentic decomposition / CRAG / self-RAG / multi-turn,
  and the deterministic GraphRAG relation graph + `GraphStore` seam.

**Solid:** structured channel, narrative hybrid retrieval, agent orchestration, office
extraction (xlsx/pptx/pdf), FastAPI + RQ service, FAQ cache, evaluation harness, ~1955 tests.

**Honest follow-ups (contributions welcome):** the depth workstreams ship the *technique + conformance*;
their **measured eval-delta** (real-model A/B ratchets on the W5 groundedness harness) is the follow-up —
a depth item isn't "done" until the ratchet shows it improved the answer. ColBERT / SPLADE / ColPali land
as **rerankers / a visual seam**; the heavy **multi-vector & sparse retrieval indexes** (and ColPali's real
GPU end-to-end) are follow-ups. The groundedness gate defaults to **offline lexical-overlap entailment**;
the real **ONNX-NLI / RAGAS** judge is opt-in behind the seam. The `VectorStore` seam is **wired live with
three real adapters** — `HybridRetriever` delegates vector scoring to it (byte-identically), it's
config-selectable (`make_vector_store` / `RAGSPINE_VECTOR_STORE`), named in `.topology()`, and behind
`[vector]` ships **`sqlite-vec`** (embedded) + **`pgvector`** (Postgres, BSD `pg8000` driver) + **`qdrant`**
(HNSW, local mode, Apache-2.0 `qdrant-client`) — all persistent and conformance-bound; still open are
**more adapters (Milvus/FAISS) and true ANN** (the adapters persist but currently score exactly in Python,
not via native HNSW/IVFFlat KNN). With `[embed-onnx]` the default is genuinely semantic (BM25 + ONNX MiniLM
→ RRF, CPU-only); with no extra it falls back to pure BM25. Pipeline-topology export (`.topology()` →
Mermaid/DOT/JSON, plus `scripts/topology.py`) ships — see [`src/ragspine/pipeline/`](src/ragspine/pipeline/).

## License

[Apache License 2.0](./LICENSE). See [`NOTICE`](./NOTICE).
