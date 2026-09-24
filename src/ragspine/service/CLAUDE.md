---
covers:
  - src/ragspine/service/
verified-against: 70032e956fbf3154d6fdf2d5dbcdb397b07a33b2
---

# service — agent contract

Auto-loaded when working under `src/ragspine/service/`. Keep terse; deep dives go in
`src/ragspine/service/docs/`.

## What lives here

`ServiceConfig` (env `RAGSPINE_*`), FastAPI app (app factory + dependency
injection), RQ task queue (`FakeQueue` tests / `RQQueue` prod), ingestion jobs
(worker-owned stores; `ServiceConfig.chunker` — default `"none"` byte-identical — rides the
narrative-ingest job payload into `ingest_narrative(chunker=make_chunker(...))`, so `parent_child`
small-to-big chunking is a config switch, ADR 0018; likewise `ServiceConfig.narrative_segment_chunking` /
`RAGSPINE_NARRATIVE_SEGMENT_CHUNKING` — default `False` byte-identical — rides the payload as `segment_chunking`;
and `ServiceConfig.persist_vectors` / `RAGSPINE_PERSIST_VECTORS` (+ `vector_db_path`, default the chunk db's
`<stem>.vectors.db`) — default `False` byte-identical — makes ingest embed chunks into a persisted sqlite-vec
`ChunkVectorIndex` (`index_narrative_vectors`, called by the facade and, via payload `persist_vectors` +
server-decided `embedding`/`retrieval_mode`/`persistence_policy`, by the worker job) and makes
`open_narrative_retriever` read it back (`open_vector_channel`); no backend / empty index degrades to BM25 with an
`op=narrative.vector_channel` / `narrative.vector_index` count trace, a model mismatch raises), FAQ short-circuit cache, and the **Dify workflow service**
(`dify/` — L0 static gate + L1/L2 safe execution; ADR 0014): endpoints
`/v1/dify/{analyze,compile,run,run/jobs}` reuse the app factory / DI / RQ queue.
The high-level local facade selects retrieval through `RetrievalProfile` and the frozen
`RetrievalPreset`: `economy` is lexical-only, `balanced` adds deterministic in-process vectors,
and `quality` opts into ONNX embeddings, cross-encoder reranking, and post-processing. `RetrievalPreset.persist_vectors`
(facade config `storage.persist_vectors`, default `False`) turns on the persisted chunk vectors above; `embedding` /
`reranker` also accept `"local-http"` (OpenAI-compatible `/v1/embeddings` / `/v1/rerank`, env `EMBEDDING_*` / `RERANK_*`).
`ServiceConfig.page_parent` / `RAGSPINE_PAGE_PARENT` (`page+child` default | `dedup` | `off` byte-identical; flipped from
`off` on the evidence in CHANGELOG Unreleased — blind review answerable@1 69% vs `dedup` 46%, real-LLM route B recall@1
.31 → .56; facade
`RAGSpine.local(retrieval=make_retrieval_preset(page_parent=…))` — kept out of `RAGSpineConfig.retrieval`, whose
effective dict is pinned to the preset recipe) is threaded by `open_narrative_retriever` into
`build_narrative_retriever(page_parent=)`: query-time only, not part of the index fingerprint.
`ServiceConfig.page_images` / `RAGSPINE_PAGE_IMAGES` (`off` default | `on`) + `page_images_top_n` (3) make
`open_narrative_retriever` wrap the final retriever in `PageImageRetriever` (needs `page_parent` ≠ `off`); ingest-side
`page_image_dpi` (144) / `page_image_max_side` (1568) / `page_image_dir` (default `<chunk db dir>/page_images`) ride the
narrative job payload; the worker re-validates `source_pdf` (payload or sidecar) before writing (`SourcePdfError` →
`JobError(stage="validation")`) and adds a count-only `page_images` block to its report when a PDF was linked. The facade
`RAGSpine.ingest(..., source_pdf=)` does the same into `<workspace>/page_images`; `IngestResult.page_image_report`
is `None` when nothing was linked. The HTTP narrative route / worker suffix allowlist (`_NARRATIVE_SUFFIXES` in
`api/routes.py` + `tasks/jobs.py`) is `.pptx/.pdf/.md`, so `.md` + `source_pdf` (sidecar or worker payload) works over
HTTP and under `allowed_upload_root`: the `.md` passes `validate_ingest_path` (resolved path inside the root + suffix) and
the linked PDF must also resolve inside the root, else `JobError(stage="validation")` before any write. There is no
content sniffing — a `.md` is only ever decoded as UTF-8 text (`errors="replace"`), never executed; no per-file size
cap exists for any narrative suffix. The structured route does not take `.md` (phase 2).
The L2 subprocess entry ships inside the wheel (`dify/run_dify_workflow.py`, `python -m`-able;
repo `scripts/` copy is a source-tree fallback). `dify/http_client.py` is the guarded client the
runner injects for http-request nodes — default-off (`RAGSPINE_DIFY_HTTP_ENABLED`), stdlib-only,
timeout-clamped, 1MB response cap, http(s)-only redirects; generated code never imports networking.

The **OpenAI Chat Completions clone** (`api/openai_public.py`, self-contained like `dify_public.py`;
`app.py` only `include_router`) exposes `POST /v1/chat/completions` (blocking + SSE) and
`GET /v1/models` in the official OpenAI shape so unmodified OpenAI-compatible clients can treat
RAGSpine as a model. It reuses the `/v1/ask` guard chain (FAQ short-circuit → `answer_question`)
rather than reimplementing it, and inherits **guard-before-stream**. `messages` map to
`question` (last `user` turn) + `history` (earlier `user`/`assistant` turns); client `system`
messages are dropped — the system prompt is server-controlled (prompt-injection boundary).
Provenance rides a non-standard top-level `ragspine` field (`request_id`/`route`/`sources`) on the
blocking body and on the final stream chunk; `usage` is a documented character-count approximation,
never a fabricated tokenizer count.

The separate **offline workflow catalog/scaffolder** is configuration-only and never executes a
workflow: `GET /v1/workflow-templates` returns metadata-only pages (`offset`, `limit <= 100`,
`total`, `next_offset`) with a page-specific weak ETag and public `max-age=300,
stale-while-revalidate=3600`; its list rows deliberately omit YAML, the full workflow, and previews.
`GET /v1/workflow-templates/{template_id}` returns the selected canonical Dify document plus YAML and
a versioned graph-only preview; `POST /v1/workflow-scaffold` either reuses a bundled template or builds
the bounded offline fallback and returns the same preview contract. Preview projection exposes only
node/edge identity, labels, geometry, containment, and branch labels—never prompts, provider config,
variables, or credentials. `ServiceConfig.workflow_matcher` is constructed during app lifespan, stored
on `app.state`, and obtained through DI; unavailable semantic matching falls back to the lexical matcher.

`studio/launch.py` backs the read-only launch-session endpoint `GET /v1/launch-sessions/{id}`:
an in-memory, bounded (FIFO, max 8), thread-safe registry populated by the 127.0.0.1-only CLI
`workflow serve` and read back by the Studio frontend via an opaque `secrets.token_urlsafe` token.
It never executes a workflow and its contents (name/YAML) never enter observability traces or logs;
unknown/overlong/non-token ids get an identical 404.

`conversation.py` is the **W6c multi-turn skeleton (opt-in, programmatic)**: `ConversationMemory` (bounded,
stores only the prior turn's home entity-code + period — non-sensitive) + `resolve_followup` (deterministic
carry-forward of those slots into a structured/composite follow-up that omits them) + `ConversationSession.ask`
(re-runs the **full** `answer_question` every turn — the security gate re-screens the augmented question; a
competitor follow-up is still refused, home context is never carried into an out-of-scope question, a refused
turn is never remembered). Not yet endpoint-wired (follow-up). Opt-in config knobs feed the agent path, all
default `"none"` ⇒ the agent/retriever path is **byte-identical**: `ServiceConfig.query_decompose` (W6a,
`make_decomposer` in `routes.py`), `ServiceConfig.corrective` (W6b, `make_corrective_retriever` in
`open_narrative_retriever`), `ServiceConfig.query_transform` (W9 HyDE / RAG-Fusion / step-back, `make_query_transform`
wrapping the base retriever in `open_narrative_retriever`, upstream of the corrective wrap — needs a provider), and
`ServiceConfig.adaptive` (W9 Adaptive-RAG complexity routing, `make_adaptive_decomposer` in `routes.py` — when set
it selects the decomposer instead of `query_decompose`).

Built on the family core `corespine`: `ServiceConfig.from_env` uses `load_from_env`
(3 legacy env aliases preserved); the task queue re-exports `corespine.JobStatus`,
its `TaskQueue` Protocol extends `corespine.TaskQueue`, and `JobError` / `PathNotAllowedError`
inherit `CorespineError` with stable codes. External error shape `{type,message,stage,retryable}`
is unchanged (normalized via `error_to_dict`).

`/v1/ask/stream` is the **SSE variant of `/v1/ask`** (`routes.py` `ask_stream`, returns
`StreamingResponse` `text/event-stream`), driven by the `StreamingProvider` seam
(`agent/llm_provider.py`: a `@runtime_checkable` Protocol adding only `chat_stream(...) -> Iterator[str]`
alongside `LLMProvider.chat`, plus `iter_text_chunks` / `STREAM_CHUNK_CHARS`; `MockProvider` satisfies it).
Events: `{"type":"start",request_id}` → one `{"type":"delta","text":chunk}` per `iter_text_chunks(answer)`
→ `{"type":"done",...route/answer_kind/clarification/sources/tool_status_summary/cache}`, framed
`data: {json}\n\n` (same idiom as `dify_public._sse_iter`).
Both `/v1/ask` and `/v1/ask/stream` accept an optional `AskRequest.history` (`list[(role, text)]`,
default `None`) passed straight through to `answer_question(history=)` (ADR 0017, same semantics):
generation-context only, never intent-parsing input, no new evidence. Default-absent ⇒ byte-identical.

**Invariant — guard-before-stream**: the anti-fabrication guard runs to completion (the not_found→refusal
rewrite is applied) **before the SSE stream opens** — the whole guarded compute (FAQ short-circuit →
`answer_question` → derive answer/route/answer_kind/sources/cache → emit trace) happens in the handler body,
wrapped in `try/except → _error_response(500)` (a pre-stream failure is a normal JSON 500, never a half-open
stream); the generator streams only the already-guarded `AgentResult.answer` and makes **no** provider/store
calls, so a not-found answer can only ever stream the refusal.

## Invariants

- **HTTP ingress is bounded before parsing.** The app-level receive wrapper rejects request bodies over
  2 MiB before Starlette/Pydantic buffering, and 422 responses never reflect submitted `input`/`ctx`
  values. Keep this outer cap and opaque validation shape when adding body-bearing endpoints.
- **FAQ conservative exclusions** — structured-numeric / competitor / real-time /
  expired / disabled / RESTRICTED content must never short-circuit. The FAQ layer
  sits in front of the anti-fabrication guard, so a wrong short-circuit bypasses it.
- **Workflow catalog/scaffold is a read-only trust boundary.** It accepts no provider, API key, URL,
  arbitrary path, or install request and never runs generated code. List responses stay metadata-only;
  detail/scaffold previews must continue through the bounded public graph projection rather than
  copying arbitrary Dify node data.
- **Dify run is a trust boundary** — `/v1/dify/{analyze,compile}` never execute (always
  safe); `/v1/dify/run[/jobs]` is default-off (`dify_run_enabled=False` → 403) and, when
  on, always passes L0 static gate (warnings reject + import allowlist) → L1 restricted
  builtins sandbox (no open/os/network; `__build_class__` + guarded `__import__`) → (Linux)
  L2 subprocess + SIGKILL + setrlimit. The `provider` is server-decided; clients can never
  inject `provider_expr` (isolated process / worker rebuild it via `build_provider`).

## Read before editing

- **HTTP is a boundary adapter — don't re-home business logic here.** `api/app.py` (app
  factory) wires `config`/`provider`/`queue`/`faq_cache`/`workflow_matcher` onto `app.state`; `api/dependencies.py`
  reads them back, all overridable via `app.dependency_overrides` in tests. Route handlers
  (`api/routes.py`) adapt at the edge and call into `agent`/`retrieval` — never read env, build a
  provider, or reimplement `answer_question` inside a handler. Pull collaborators through the
  `get_*` deps, not from module globals.
- **Stores & providers are per-request / per-job, never global singletons.**
  `config.open_fact_store` / `open_narrative_retriever` are context managers that open **and
  close** within one request; `tasks/jobs.py` opens *worker-owned* stores from the payload's paths
  and closes them in `finally`. Don't cache a sqlite connection across requests or reuse the
  caller's connection inside a worker.
- **The opt-in agent seams default to `"none"` and the no-injection path stays byte-identical.**
  `open_narrative_retriever` composes them in a fixed order — `query_transform` (W9) wraps the base
  retriever **upstream of** the `corrective` (W6b) wrap; `query_decompose` (W6a) / `adaptive` (W9)
  pick the decomposer in `routes.py`. Every knob defaults so the agent/retriever path is bit-stable
  (see `agent/` + `retrieval/` CLAUDE.md for the byte-identity contract). Adding or reordering a
  seam must not perturb the default path.
- **`provider` is server-decided; a client can never inject it.** `config.provider_config_dict`
  returns only serializable provider *config* (no instance, no `provider_expr`) — the dify isolated
  process / RQ worker rebuild it via `build_provider`. Never add a provider instance or
  `provider_expr` to a serialized payload (Dify trust boundary, above).
  `provider_type` is `mock` | `anthropic` | `claude-cli` (eval-only local `claude -p`; its model is
  the separate `claude_cli_model` / `RAGSPINE_CLAUDE_CLI_MODEL`, default unset — it does **not**
  inherit the anthropic `model` default).
- **Ingest-path validation is defense-in-depth — re-run it in the worker.**
  `config.validate_ingest_path` (allowed-upload-root + suffix allowlist) runs at enqueue **and
  again** in `tasks/jobs.py` before landing; the worker never trusts the enqueuer. `PathNotAllowedError`
  / `JobError` inherit `CorespineError` with stable codes — keep the external
  `{type,message,stage,retryable}` error shape (normalized via `error_to_dict`) unchanged.
- **Legacy env aliases are load-bearing.** `ServiceConfig.from_env` rewrites 3 irregular legacy
  keys (`RAGSPINE_PROVIDER` / `_COMPANY_PROFILE` / `_FAQ_SOURCE`) to canonical field names and falls
  back `db_path` → `data/fact_metric.db`; `corespine.load_from_env` derives the rest by
  `PREFIX_FIELDNAME`. Renaming a field silently breaks env compat — add fields, don't rename.
- **`FakeQueue` and `RQQueue` must stay behaviour-parallel.** Both honour the same `enqueue`
  signature (RQ-only kwargs `timeout`/`max_retries`/`result_ttl`/`failure_ttl`); `FakeQueue` runs the
  job inline and is idempotent on an explicit `job_id`; `rq`/`redis` are lazy-imported so the module
  imports (and offline tests run) without them. `JobStatus` is re-exported from `corespine` — don't
  fork its shape.

## Deep dives

<!-- none yet -->
