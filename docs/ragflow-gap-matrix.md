# RAGFlow gap matrix — engine capabilities vs. RAGFlow v0.27.2

> **status:** snapshot · **checked:** 2026-10-07 · **baseline:** RAGFlow **v0.27.2** (latest stable, 2026-09-10);
> **v1.0.0-rc1** (2026-09-29, Go rewrite: DeepDoc CPU-only, local sandbox removed, Team/Me permissions not yet
> supported) is noted where it changes a row.
> 对照表，不是代码描述：与 [`prd-quality-depth.md`](prd-quality-depth.md) 一样**不带 `covers:` frontmatter**，不进
> drift 追踪；每行 status 用模块路径 / ADR / W 编号作依据，代码变了需手工重核。
> Columns and legend match the [Gap matrix (depth)](prd-quality-depth.md#gap-matrix-depth): **Quality stage** = the
> RAGFlow capability; **Today** = what ragspine (`src/ragspine/` + sibling `src/enterprise_pdf_rag/`, "EPR") has at
> `main @ 26de842`; **Target** = what RAGFlow does, with a source key; **WS · Phase** = the ragspine workstream / ADR that
> carries it (`—` = none yet). `EPR ADR NNNN` = `docs/enterprise-pdf-rag/adr/NNNN-*.md`; `ADR NNNN` = `docs/adr/NNNN-*.md`.

## Sources

| Key | URL |
|---|---|
| README | https://github.com/infiniflow/ragflow |
| REL | https://github.com/infiniflow/ragflow/releases (v1.0.0-rc1, v0.27.2, v0.27.1, v0.27.0, v0.26.2–v0.26.4) |
| CFG | https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configuration.md |
| CHAT | https://github.com/infiniflow/ragflow/blob/main/docs/guides/chat/chat_configuration.md |
| KC | https://github.com/infiniflow/ragflow/blob/main/docs/guides/knowledge_compilation/overview.md |
| META | https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/metadata_management.md |
| TEST | https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/retrieval_testing.md |
| PIPE | https://github.com/infiniflow/ragflow/blob/main/docs/guides/agent/ingestion_pipeline/understand_core_ingestion_pipeline_components.md |
| AGENT | https://github.com/infiniflow/ragflow/tree/main/docs/guides/agent/agent_workflow (basic / flow / data / dialogue / tool components) |
| MCP | https://github.com/infiniflow/ragflow/blob/main/docs/develop/mcp/overview.md |
| API | https://github.com/infiniflow/ragflow/blob/main/docs/references/http_api_reference.md |
| DS | https://github.com/infiniflow/ragflow/blob/main/docs/guides/data_source/data_source_categories_and_selection.md |
| MEM | https://github.com/infiniflow/ragflow/blob/main/docs/guides/memory/configure_memory.md |
| TEAM | https://github.com/infiniflow/ragflow/blob/main/docs/guides/team/permission_system_overview/index.md |
| TRACE | https://github.com/infiniflow/ragflow/blob/main/docs/administrator/tracing.mdx |
| SANDBOX | https://github.com/infiniflow/ragflow/blob/main/docs/administrator/admin/admin_ui/configure_code_execution_sandbox.md |
| CHAN | https://github.com/infiniflow/ragflow/blob/main/docs/guides/chatchannel/chat_channels_overview.md |
| DOCENG | https://github.com/infiniflow/ragflow/blob/main/docs/develop/switch_doc_engine.md |

ragflow.io/docs/dev/ pages returned 404 / empty on 2026-10-07; the same docs were read from the repo's `docs/` tree.
Note: since v0.27.0 RAGFlow's UI deprecates **GraphRAG** and **RAPTOR** in favour of *Knowledge Compilation* (Graph / Tree /
PageIndex / Wiki / MindMap / Timeline / To-Skills); old indexes stay searchable [KC, REL].

Legend: **kind** 🛡/⭐/🔧 · **status** ✅ have · ◐ partial · ✗ gap.

## 1. Document parsing (DeepDoc)

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| OCR for scanned pages | family OCR `pdfspine→ocrspine` default, scanned path wired (`extraction/extractors/pdf_scanned_extractor.py`) | DeepDoc OCR in the default parser; PaddleOCR / PaddleOCR-VL / Mistral OCR / SoMark selectable [CFG, REL] | 🛡⭐ | ✅ | W3a |
| Layout recognition | default = per-page model layout call; `deterministic-text-pages` (EPR ADR 0028); local ONNX PP-DocLayoutV3 `"onnx-layout"` (`enterprise_pdf_rag/adapters/onnx_partition.py`) | DeepDoc layout model on every PDF page [CFG] | ⭐ | ✅ opt-in (ONNX layout is explicit-choice only, no preset selects it) | EPR ADR 0028 / 0030 |
| Table structure recognition (TSR) | ruled-grid proof (EPR ADR 0014); unruled → verbatim rows (EPR ADR 0027); SLANet-plus TSR grid kept `PENDING` (`adapters/pdfspine_tsr.py`); `TableStructureRecognizer` seam (`extraction/tables/`) | DeepDoc TSR ONNX on the table crop, cells filled from text boxes [CFG] | ⭐ | ✅ default-off (`unverified_table_structure="rows"` in both presets) | EPR ADR 0031 |
| Borderless / merged-cell tables | TSR (above) + Docling fallback `[pdf-docling]` (`pdf_digital_extractor.py`) + docx `gridSpan`/`vMerge` + nested grids (`docspine_extractor.py`) | DeepDoc / TCADP, or a vision model for borderless & merged cells [CFG] | ⭐ | ✅ (vision-model table reading deliberately absent: the structure seam never emits cell text) | W3d · EPR ADR 0027/0031 |
| Fast / naive parse mode | lite ingest (EPR ADR 0025) + text-layer narrative extraction via pypdfium2 (`ingestion/narrative/narrative_extract.py`) | "Naive" PDF parser: plain text, no OCR / TSR / layout [CFG] | 🔧 | ✅ | EPR ADR 0025 |
| Pluggable parser backends | `GridExtractor` / `OcrBackend` / `Extractor` registry seams (`extraction/registry.py`); backends: pdfspine (default), Docling, Azure-DI markdown input (`extraction/di_markdown/`) | Docling, MinerU, OpenDataLoader, TCADP, MonkeyOCRv2, Mistral OCR, PaddleOCR-VL [CFG, REL] | 🔧 | ◐ seam + 3 backends; no MinerU / Mistral OCR / PaddleOCR-VL / MonkeyOCR adapters | prd-breadth Extractor |
| VLM page parsing | EPR model page partition (one bounded layout call per page, verbatim-span validated); ragspine narrative path has none | Vision LLM parses PDF / DOCX / PPTX / MD / images (toggle) [CFG] | ⭐ | ◐ PDF only (EPR); no VLM path for docx / pptx / md | EPR ADR 0013 · 0028 |
| PDF parse options | header/footer = running lines (`adapters/running_lines.py`), DI `PageHeader/Footer` dropped; multi-column region binding (`extraction/evidence/page/column_regions.py`); page selection only as the fixed first-20-pages milestone | multi-column, remove TOC, remove header/footer, page ranges [CFG] | 🔧 | ◐ no TOC-page removal, no general page-range option | EPR ADR 0005 · 0013 |
| Office + text formats | `.pdf` `.docx/.docm` `.pptx` `.xlsx` `.txt` `.md` (`narrative_extract.SUPPORTED_SUFFIXES`, `extraction/extractors/`) | PDF, DOC/DOCX, PPTX, Excel, TXT, Markdown [CFG, README] | ⭐ | ✅ (legacy binary `.doc` / `.ppt` not supported) | W3b/W3c |
| Web / e-book / data / image files | none — `narrative_extract` raises on other suffixes | HTML, EPUB (preview v0.27.2), CSV, JSON, image files (OCR / VLM) [CFG, REL] | 🔧 | ✗ | — |
| Audio / video | none (no ASR seam) | ASR for WAV/MP3/AAC/FLAC/OGG; video via multimodal model [CFG] | 🔧 | ✗ | — |
| Email | none | EML / MSG: headers, body, attachments (Email template) [CFG] | 🔧 | ✗ | — |

## 2. Chunking

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| General chunker knobs | paragraph-greedy + sentence split, `max_chars` / `overlap_chars` (`retrieval/chunking/chunking.py`) | token target (512), overlap ratio, user delimiters, merge-to-size [CFG] | 🔧 | ◐ char-based sizing, no user delimiter list, no token budget | W4b |
| Domain templates: Laws / Book / Q&A | `make_chunker("laws"/"book"/"qa")` (`retrieval/chunking/domain_presets.py`) + FAQ short-circuit (`service/faq/`) | Laws, Book, Q&A templates [CFG] | ⭐ | ✅ opt-in | W4b |
| Domain templates: Manual / Paper / One | `LayoutAwareChunker` heading sections approximate Manual; nothing paper-specific; "whole doc" only via a large `max_chars` | Manual, Paper (abstract/sections), One (doc = chunk) [CFG] | 🔧 | ◐ no paper-structure or one-chunk preset | W4b |
| Presentation template | per-slide locators `slide=N,frame=M` / notes (`narrative_extract`); richer `pptspine` opt-in | Presentation: one chunk per slide + slide image [CFG] | ⭐ | ✅ | W3c |
| Table template (spreadsheet rows) | structured channel: xlsx → `StyledGrid` → `fact_metric` deterministic facts (`extraction/extractors/xlsx_styled_extractor.py`, `ingestion/structured/`) | Table: each row a chunk with column-typed fields [CFG] | 🛡⭐ | ✅ (stronger: exact SQL, not text chunks; CSV not accepted) | ADR 0001 |
| Parent-child (small-to-big) | `parent_child` chunker + store-level expansion (ADR 0018); page-level `page+child` is the service default (`retrieval/page_parent/`) | "Use sub-chunks for retrieval" [CFG] | ⭐ | ✅ | ADR 0018 · W4b |
| Title chunker (hierarchy / group) | heading-path sections (`layout_chunker.py`), `segment_chunking` heading path `a > b` (`ingestion/narrative/`) | heading tree with H1–H5 regex rules; group mode merges small sections; first chunk as global context [CFG] | ⭐ | ◐ hierarchy yes; heading predicate is code not user regex; no group-merge mode | W4b |
| Table / image context window | DI md tables linearized `row \| header: value`, figure caption first; EPR chart index text (`processing/index_text.py`) | configurable N tokens of surrounding text on table & image chunks [CFG] | ⭐ | ◐ no surrounding-text window knob | EPR ADR 0012 |
| Auto-keyword | none | LLM extracts N keywords per chunk into the index [CFG] | ⭐ | ✗ | — |
| Auto-question | none (HyDE is query-side only, `agent/query_transform.py`) | LLM generates N questions per chunk, indexed [CFG] | ⭐ | ✗ | — |
| Augmented context per chunk | deterministic context header via `index_text_fn` (`retrieval/contextual.py`, `RAGSPINE_CONTEXTUAL_INDEX`); LLM adapter = seam only | LLM writes a contextual summary per chunk [CFG] | ⭐ | ◐ deterministic only; LLM contextualizer not shipped | W4a |
| Auto tags / tag sets | none | chunks + queries tagged against a tag-set dataset, used as a ranking feature [CFG] | ⭐ | ✗ | — |
| Auto metadata | EPR page metadata: LLM or deterministic, values verified verbatim (`adapters/page_metadata_extraction.py`); ragspine narrative path has no ingest-time metadata generator | LLM- or parser-generated metadata fields per document [CFG, META] | ⭐ | ◐ PDF (EPR) only | EPR ADR 0013 |

## 3. Indexing & retrieval

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| Hybrid retrieval | BM25 (CJK uni+bigram) + dense via `VectorStore` → RRF (`retrieval/lexical/`, `retrieval/vector/`); EPR `adapters/hybrid_search.py` | full-text + embedding per Indexer [CFG, CHAT] | ⭐ | ✅ | W1 |
| Weighted fusion | rank-based RRF only | "vector similarity weight" blends keyword and vector scores [CHAT] | 🔧 | ◐ no score-weight knob (RRF by design) | — |
| Similarity threshold | `top_k` only; relevance cut-offs exist only inside opt-in CRAG grading (`retrieval/corrective.py`) | similarity threshold + Top-N on every retrieval [CHAT, TEST] | 🔧 | ◐ no absolute score floor on the default path | W6b |
| Rerank model | local cross-encoder, `/v1/rerank` HTTP, LLM listwise, ColBERT / SPLADE (`retrieval/rerank/`, `make_reranker`) | optional rerank model (incl. Bedrock) [TEST, REL] | ⭐ | ✅ opt-in | W2 · W11 |
| Indexed-field choice | `contextual_index=off\|heading\|full` decides the embedded/BM25 text (`retrieval/contextual.py`) | index processed text / generated questions / augmented context; filename embedding weight [CFG] | ⭐ | ◐ no question field, no filename weighting | W4a |
| Per-dataset PageRank | none (multi-library fusion is pure RRF) | dataset-level score added to hybrid similarity [CFG] | 🔧 | ✗ | — |
| Metadata filtering | manual `MetadataFilter` pre-scoring + opt-in automatic extraction from the query (`retrieval/filtering/`); EPR period / region pre-filters | metadata conditions narrow retrieval, pushed down to the metadata index [META, REL] | 🛡⭐ | ✅ (automatic extractor opt-in) | EPR ADR 0013 |
| Cross-language search | LLM query translation as an extra query, default `auto` (`retrieval/translation/`) | target languages for cross-lingual matching [CHAT, TEST] | ⭐ | ✅ | EPR ADR 0018 |
| Multi-dataset retrieval | `MultiIndexRetriever` RRF + `library_id` provenance, opt-in router (`retrieval/routing/`); EPR cross-document corpus | Chat / Search / Agent over several datasets [CHAT] | ⭐ | ✅ | EPR ADR 0032 |
| Store engines | `VectorStore` seam: sqlite-vec / pgvector / Qdrant + entry points (`retrieval/vector/adapters/`) | Elasticsearch default, Infinity, GaussDB, SereneDB [DOCENG, REL] | 🔧 | ✅ (no ES / Infinity adapter; the seam covers it) | prd-breadth VectorStore |
| Knowledge graph (Graph compilation / GraphRAG) | W7a deterministic relation graph + query API, W7b LLM extract → connected-components communities → synthesis summaries, `GraphStore` seam, MS GraphRAG artifact interop (`graph/`, `compat/graphrag.py`) | LLM entity/relation graph with configurable specs, communities, entity resolution, graph retrieval [KC] | ⭐ | ◐ no Leiden / entity resolution / global query; not wired into `answer_question` | W7a/W7b/W7c |
| RAPTOR / Tree compilation | deterministic threshold-cluster tree + `is_synthesis` summaries (`retrieval/raptor.py`) | RAPTOR (deprecated) → Tree compilation, searchable [KC] | ⭐ | ◐ tree built; retrieval-time tree traversal / collapsed-tree search not done | W10 |
| PageIndex (TOC-tree reasoning) | deterministic outline at ingest + one bounded model call picking sections; summaries never evidence (`enterprise_pdf_rag/adapters/tree_retrieval.py`) | PageIndex compilation [KC] | ⭐ | ✅ (EPR PDF path only) | EPR ADR 0019 |
| Wiki / MindMap / Timeline / To-Skills | none | LLM-compiled document/dataset artifacts [KC, REL] | 🔧 | ✗ | — |
| Retrieval testing | `eval/retrieval_only.py`, `cli/eval_retrieval_ab.py`, EPR `adapters/retrieval_testbench.py` | dataset retrieval-testing page with its own parameters [TEST] | 🛡 | ✅ (CLI / eval harness, no UI) | W5 |

## 4. Generation & citation

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| Citations to source documents | provenance invariant: every fact / snippet carries `source_doc_id` + locator; merged suffix (`agent/citations.py`) | "Show citations": cited chunks + source docs [CHAT, README] | 🛡 | ✅ | ADR 0029 |
| Citation location / highlight | EPR `ClaimCitation` = page + bbox + verbatim quote + table row/col/header (`answers/models.py`), source-review HTML; ragspine narrative = page / para / slide locators | chunk metadata in citations, Excel cell location, retrieval highlighting [CHAT, REL] | 🛡 | ◐ bbox-level only on the EPR PDF path; narrative path stops at para / page | EPR ADR 0014 |
| Empty-response behaviour | deterministic not-found; narrative fallback kept only if grounded; number guard rewrites ungrounded numbers (`agent/agent.py`, `agent/number_guard.py`) | preset reply when nothing retrieved; blank ⇒ model answers freely [CHAT] | 🛡 | ✅ (stricter: never falls back to model knowledge) | ADR 0023 / 0024 |
| Prompt configuration | system prompts are code constants (`agent/agent.py` `_SYSTEM_PROMPT_TEMPLATE`, `enterprise_pdf_rag/answers/prompt.py`) | editable system prompt with `{knowledge}` placeholder per assistant [CHAT] | 🔧 | ◐ ADR 0008 PromptRegistry accepted but no user-editable prompt surface | ADR 0008 |
| Multi-turn query rewrite | W6c bounded memory + deterministic slot carry-forward (`service/conversation.py`); history is generation-only | "multi-turn optimization": LLM rewrites the query from history [CHAT] | ⭐ | ◐ no LLM coreference rewrite | W6c · ADR 0017 |
| Question keyword analysis | deterministic controlled-vocab intent + synonym multi-query (`agent/intent.py`); RAG-Fusion opt-in | LLM keyword extraction from the question to boost retrieval [CHAT] | ⭐ | ◐ no LLM keyword step (deterministic analogue only) | W9 |
| Agentic RAG thinking modes | decomposition (`agent/decompose.py`), CRAG loop (`retrieval/corrective.py`), HyDE / RAG-Fusion / step-back / Adaptive-RAG (`agent/query_transform.py`) — each opt-in, separately switched | None/Low/Medium/High/Ultra levels bundling rewrite, decomposition, evidence checks [CHAT, REL] | ⭐ | ◐ pieces shipped; no single depth-level orchestration / preset | W6a/W6b/W9 |

## 5. Multimodal

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| Image understanding inside documents | EPR figure / chart / diagram / formula semantics, values re-verified against SVG / text (`adapters/chart_semantics.py`, `visual_semantics.py`); nothing for images embedded in docx / pptx / md | VLM describes images in PDF / DOCX / PPTX / MD at parse time [CFG] | ⭐ | ◐ PDF (EPR) only | EPR ADR 0002/0006/0015 |
| Page images & image-aware retrieval | page PNGs attached to top snippets for the generator, `off\|tagged\|all` (`retrieval/page_images/`, ADR 0025); ColPali page-as-image retriever (`retrieval/vision/colpali.py`) | image chunks with context window; `image_update_mode` on the chunk API [CFG, REL] | ⭐ | ✅ opt-in (ahead of RAGFlow on page-image retrieval) | ADR 0025 · W12 |

## 6. Interfaces & ops (engine side)

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| HTTP API | FastAPI: `/v1/ask`, `/v1/ask/stream`, structured / narrative ingest jobs, `/v1/jobs/{id}`, topology (`service/api/routes.py`) | datasets, documents, chunks CRUD, retrieval-only, chats, sessions, agents, files [API] | 🔧 | ◐ no dataset / document / chunk management or retrieval-only endpoint | ADR 0019 |
| OpenAI-compatible API | `/v1/chat/completions` (+ stream) and `/v1/models`, sources in a `ragspine` extension field (`service/api/openai_public.py`) | OpenAI-compatible chat & agent completions [API] | 🔧 | ✅ | — |
| Python SDK | `RAGSpine` facade (`facade.py`, `session.py`), LightRAG shape clone (`compat/lightrag.py`) | Python SDK over the HTTP API [REL] | 🔧 | ✅ | ADR 0019 |
| MCP server | none — RAG is exposed via OpenAI / Dify / n8n compat routes, not MCP | RAGFlow as MCP server (retrieve, list datasets / chats) [MCP] | 🔧 | ✗ | — |
| Composable ingestion pipeline | code-level seams `SourceConnector` → `Extractor` → `Chunker` → `EmbeddingBackend` → `VectorStore`, topology export (`pipeline/`) | Parser / Transformer / Chunker / Indexer / Compiler pipeline per dataset [PIPE] | 🔧 | ✅ (as code seams; the visual editor is out of scope) | ADR 0019 |
| Data source connectors | Filesystem / InMemory / HTTP / Notion behind `SourceConnector` (`ingestion/source/`) | ~40 connectors: Confluence, S3, Drive, SharePoint, Jira, Slack, DBs, Sitemap… [DS] | 🔧 | ◐ 4 connectors; S3 / Drive / Confluence etc. only via the seam | prd-breadth SourceConnector |
| Tracing export | privacy-aware traces + per-call LLM telemetry, OTel adapter (`common/observability/`) | Langfuse chat traces & generation observations [TRACE] | 🛡 | ✅ (codes / counts / timings only by invariant; no prompt / answer text, unlike Langfuse) | ADR 0028 |

## Out of scope by family boundary

Per the family charter (`spine/CLAUDE.md`, `docs/spine-family.md`, family ADR 0001): product concepts live in
**spinestudio** (app layer), multi-agent / tool orchestration in **spineagent** (which composes ragspine as a Tool / MCP
server at runtime). These RAGFlow features are listed for completeness and **not counted** as engine gaps.

| RAGFlow feature | Source | Owner | Note |
|---|---|---|---|
| Agent canvas, Begin triggers (conversational / task / webhook) | AGENT | spineagent / spinestudio | ragspine only compiles / runs Dify & n8n workflows for migration (`dify/`, `n8n/`) |
| Flow & data components (Switch, Iteration, Loop, Categorize, Variable Assignor…) | AGENT | spineagent | — |
| Agent component with tools, sub-agents, JSON-schema output | AGENT | spineagent | — |
| Agent tools: web search (Tavily, Google, DuckDuckGo, SearXNG…), ArXiv / PubMed, SQL, Email, HTTP, Browser | AGENT | spineagent | web search also conflicts with ragspine's offline-first default |
| Web search inside chat | CHAT, REL | spineagent / spinestudio | — |
| Code executor sandbox (gVisor / E2B / Aliyun) | SANDBOX | spineagent | ragspine's `service/dify/runner.py` sandbox only executes compiled Dify workflows |
| MCP client inside agents | MCP | spineagent | the MCP *server* row is kept in the engine matrix |
| Agent memory (raw / semantic / episodic / procedural) | MEM | spineagent | ragspine W6c conversation memory is engine-side and counted above |
| Chat channels (WhatsApp, DingTalk, WeCom, Feishu, Telegram…) | CHAN | spinestudio | — |
| Team / tenant / sharing permissions | TEAM | spinestudio | ragspine keeps only the RESTRICTED sensitivity invariant |
| Admin UI, Go CLI, dataset / chat management UI, Search app UI | REL, TEST | spinestudio | — |
| TTS / ASR in chat, multi-model side-by-side comparison | CHAT | spinestudio | — |
| Image upload in chat for vision models | REL | spinestudio | — |
| Visual ingestion-pipeline editor | PIPE | spinestudio | the code-level pipeline is counted above |

## Summary

Engine rows: **56** — ✅ **25** · ◐ **22** · ✗ **9** (the 14 out-of-scope rows above are not counted).

| Section | Rows | ✅ | ◐ | ✗ |
|---|---:|---:|---:|---:|
| 1. Document parsing | 12 | 6 | 3 | 3 |
| 2. Chunking | 13 | 4 | 6 | 3 |
| 3. Indexing & retrieval | 15 | 8 | 5 | 2 |
| 4. Generation & citation | 7 | 2 | 5 | 0 |
| 5. Multimodal | 2 | 1 | 1 | 0 |
| 6. Interfaces & ops | 7 | 4 | 2 | 1 |

Top 5 worth doing (from ✗ / ◐), judged against ragspine's position — framework-free, anti-fabrication, provenance,
offline-first:

1. **RAPTOR tree retrieval (◐, W10)** — the tree already exists; collapsed-tree / traversal search is the missing half,
   is deterministic, and `is_synthesis` nodes already keep summaries out of the citable set.
2. **Auto-question + auto-keyword index enrichment (✗)** — cheapest recall win RAGFlow has; fits the existing
   `index_text_fn` layering (index text only, chunk text and citations untouched), so LLM output never becomes evidence.
3. **Knowledge graph into the answer path (◐, W7a/W7b)** — wire `GraphQuery` into `answer_question` and add entity
   resolution; the deterministic structural graph is a provenance-clean differentiator RAGFlow's LLM graph lacks.
4. **Citation location on the narrative path (◐)** — carry bbox / cell coordinates (already modelled in EPR
   `ClaimCitation`) through ragspine snippets; provenance is the core invariant and this is where RAGFlow is visibly ahead.
5. **Retrieval-only + document / chunk management API, and an MCP server (◐ / ✗)** — a thin service surface lets
   spineagent and external agents consume ragspine as a tool without the answer layer; low risk, offline, no new model.
