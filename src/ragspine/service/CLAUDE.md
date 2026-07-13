---
covers:
  - src/ragspine/service/
verified-against: e6d36368639903633ceb4d142684324cc12bce57
---

# service — agent contract

Auto-loaded when working under `src/ragspine/service/`. Keep terse; deep dives go in
`src/ragspine/service/docs/`.

## What lives here

`ServiceConfig` (env `RAGSPINE_*`), FastAPI app (app factory + dependency
injection), RQ task queue (`FakeQueue` tests / `RQQueue` prod), ingestion jobs
(worker-owned stores), FAQ short-circuit cache, and the **Dify workflow service**
(`dify/` — L0 static gate + L1/L2 safe execution; ADR 0014): endpoints
`/v1/dify/{analyze,compile,run,run/jobs}` reuse the app factory / DI / RQ queue.

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
**Invariant — guard-before-stream**: the anti-fabrication guard runs to completion (the not_found→refusal
rewrite is applied) **before the SSE stream opens** — the whole guarded compute (FAQ short-circuit →
`answer_question` → derive answer/route/answer_kind/sources/cache → emit trace) happens in the handler body,
wrapped in `try/except → _error_response(500)` (a pre-stream failure is a normal JSON 500, never a half-open
stream); the generator streams only the already-guarded `AgentResult.answer` and makes **no** provider/store
calls, so a not-found answer can only ever stream the refusal.

## Invariants

- **FAQ conservative exclusions** — structured-numeric / competitor / real-time /
  expired / disabled / RESTRICTED content must never short-circuit. The FAQ layer
  sits in front of the anti-fabrication guard, so a wrong short-circuit bypasses it.
- **Dify run is a trust boundary** — `/v1/dify/{analyze,compile}` never execute (always
  safe); `/v1/dify/run[/jobs]` is default-off (`dify_run_enabled=False` → 403) and, when
  on, always passes L0 static gate (warnings reject + import allowlist) → L1 restricted
  builtins sandbox (no open/os/network; `__build_class__` + guarded `__import__`) → (Linux)
  L2 subprocess + SIGKILL + setrlimit. The `provider` is server-decided; clients can never
  inject `provider_expr` (isolated process / worker rebuild it via `build_provider`).

## Read before editing

- **HTTP is a boundary adapter — don't re-home business logic here.** `api/app.py` (app
  factory) wires `config`/`provider`/`queue`/`faq_cache` onto `app.state`; `api/dependencies.py`
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
