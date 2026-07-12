# PRD — Orchestration Compatibility: Dify / n8n ingress, the Studio console & external-API shape cloning

> **status:** in progress (Dify compiler+runner shipped [ADR 0013/0014]; per-node execution trace shipped;
> n8n↔Dify conversion domain shipped; Studio editor shipped; Dify official + n8n public **API shape clones**
> shipped; 5 extended nodes: **frontend editing shipped, backend execution pending**) · **created:** 2026-07-12
> · **methodology:** TDD (parse/convert conformance + API-shape tests first)
> Living backlog — the ingress surfaces and console features tracked here land incrementally; carries no
> `covers:` frontmatter (each shipped piece's contract lives in its domain `CLAUDE.md` / ADR).
> Realizes [ADR 0013](adr/0013-dify-workflow-compiler.md) (Dify YAML → pure-Python compiler) and
> [ADR 0014](adr/0014-dify-workflow-service-and-safe-execution.md) (service + safe execution three layers),
> operating within [ADR 0009](adr/0009-dependency-and-framework-policy.md) (no orchestration lock-in).
> **Companion:** [`prd-breadth-via-adapters.md`](prd-breadth-via-adapters.md) rents the RAG commodity surface;
> [`prd-quality-depth.md`](prd-quality-depth.md) out-engineers the RAG quality stages. This PRD owns the
> **orchestration ingress**: how a workflow *authored elsewhere* (Dify, n8n) or *in our own console* reaches the
> deterministic RAGSpine engine, and how existing SDKs/clients reach it **without changing their code**.

## Problem statement

The mature workflow platforms — **Dify** and **n8n** — win adoption on their *authoring surface*: a visual
editor, a portable workflow format, and a public API that a large ecosystem of SDKs and clients already speaks.
RAGSpine's value is the opposite of theirs — a **framework-free, deterministic, invariant-enforcing** backend
engine (anti-fabrication, provenance, RESTRICTED isolation; see `docs/invariants.md`) — but that value is only
reachable if a user can *get their workflow in*. A team that already has a Dify DSL, an n8n workflow, or a
client written against either platform's REST API should be able to point it at RAGSpine and have it run,
compiled down to auditable pure Python behind our safety gates.

**The need:** meet the incumbents' authoring formats and API shapes at the boundary — import their workflow
definitions, speak their public API verbatim — while every execution still funnels through the one
deterministic, gated engine. Compatibility at the *edge*; our invariants at the *core*.

## Strategy (the decision)

> Don't reimplement Dify/n8n. **Clone their ingress shapes; compile everything down to the one engine.**

Three ingress layers, each a thin, conformance-tested boundary over the existing Dify compile→run pipeline
(ADR 0013/0014). None of them forks execution — they all converge on `compile_dify_yaml` + the safe runner:

| Layer | What it clones | Converges on |
|---|---|---|
| **Format ingress** | Dify DSL YAML (native) · n8n workflow JSON (converted) | the Dify IR → codegen → L0/L1/L2 runner |
| **API shape** | Dify official `/v1/workflows/*` · n8n public `/api/v1/*` + `/webhook/*` | same compile+run, re-shaped request/response |
| **Console** | the visual editor experience (Studio) | the same `/v1/dify/*` endpoints |

The gate is uniform: **every** execution path — native `/v1/dify/run`, converted `/v1/n8n/run`, cloned
`/v1/workflows/run`, webhook-triggered — respects `RAGSPINE_DIFY_RUN_ENABLED` and passes through the L0 static
gate + L1 restricted sandbox (+ L2 subprocess on Linux). A compatibility layer can add an *ingress shape*; it
can never add an *execution bypass*.

## Capability matrix

Legend: **status** ✅ shipped · ◐ partial · ✗ gap.

| Surface | Piece | Status | Where |
|---|---|---|---|
| **Dify format** | YAML → pure-Python compiler + static analyzer | ✅ | `src/ragspine/dify/` (ADR 0013) |
| | analyze / compile / run + async job endpoints | ✅ | `service/api/routes.py` (ADR 0014) |
| | safe execution L0 static gate / L1 sandbox / L2 subprocess | ✅ | `service/dify/{safety,runner}.py` |
| | **per-node execution trace** (status/timing/IO, privacy-gated) | ✅ | `dify/codegen/emitter.py` + `service/dify/tracing.py` |
| **n8n format** | n8n JSON ↔ Dify DSL bidirectional lossless conversion | ✅ | `src/ragspine/n8n/` |
| | `POST /v1/n8n/convert` · `POST /v1/n8n/run` | ✅ | `service/api/routes.py` |
| **Dify API clone** | `POST /v1/workflows/run` (blocking + SSE streaming) | ✅ | `service/api/dify_public.py` |
| | `GET /v1/workflows/run/{id}` · `/v1/info` · `/v1/parameters` | ✅ | `service/api/dify_public.py` |
| | Bearer app-key registry (`RAGSPINE_DIFY_PUBLIC_APPS`) | ✅ | `service/config.py` |
| **n8n API clone** | `/api/v1/workflows` CRUD + activate/deactivate | ✅ | `service/n8n_public/` |
| | `/api/v1/executions` · `X-N8N-API-KEY` auth · cursor paging | ✅ | `service/n8n_public/` |
| | `POST /webhook/{path}` triggers active workflow → convert → run | ✅ | `service/n8n_public/router.py` |
| **Studio console** | React Flow editor: Dify YAML ⇄ canvas lossless round-trip | ✅ | `studio/` |
| | execution visualization (node badges/timing/edge highlight/IO) | ✅ | `studio/src/pages/workflows/` |
| | undo/redo · copy-paste · multi-select · inline `{{#var#}}` autocomplete | ✅ | `studio/` |
| | drag-to-add node picker · canvas search · snap grid · templates | ✅ | `studio/` |
| | n8n JSON import (convert preview) / export | ✅ | `studio/src/pages/workflows/modals/` |
| **Extended nodes** | 5 nodes (http-request, variable-aggregator/-assigner, document-extractor, loop) — **canvas + forms + variable inference** | ✅ | `studio/src/workflow/registry.ts` + `forms/` |
| | same 5 nodes — **compile/execute (parse/ir/codegen/runner)** | ✗ | `src/ragspine/dify/` — **next** |

## What shipped this cycle (2026-07)

1. **Per-node execution trace** — codegen optionally injects a trace collector (default off → byte-identical
   generated source); the runner injects a `perf_counter` clock (sandbox builtins / import whitelist unchanged),
   and every run returns `node_traces` (index/node_id/title/node_type/status/elapsed_ms/inputs/outputs/error).
   Traces are sanitized (2000-char truncation, JSON-safe downgrade) and **only** enter the API response — never
   the privacy-gated `TraceSink` (counts-only invariant preserved). The failed-run and L2-subprocess paths carry
   traces too.
2. **n8n compatibility domain** (`src/ragspine/n8n/`) — data-driven node mapping table (trigger↔start,
   if/switch↔if-else, code↔code, langchain↔llm, set↔template-transform, noOp splice), expression conversion
   (`={{ $json.f }}` / `$node["N"].json["f"]` ↔ `{{#node_id.f#}}`), round-trip preservation via `data._n8n` /
   `x_n8n` (unmappable data preserved + warned, never silently dropped).
3. **External API shape clones** — Dify official Workflow API (`/v1/workflows/run` blocking + SSE that replays
   `node_traces` as `workflow_started`/`node_started`/`node_finished`/`workflow_finished`; Bearer app-key
   registry) and n8n public API (CRUD + activate + executions + `X-N8N-API-KEY` + webhook trigger, file-backed
   store). Both funnel through the existing compile+run under `RAGSPINE_DIFY_RUN_ENABLED`; error bodies match
   each platform's shape.
4. **Studio console** — the full visual editor (see matrix), 210 vitest + `tsc --noEmit` green, mounted at
   `/studio` by the service.

## Remaining backlog / next steps

- **P0 — `[backend]` execute the 5 extended nodes.** The frontend can author http-request /
  variable-aggregator / variable-assigner / document-extractor / loop today, but the backend lowers them to
  `UnsupportedNode` (L0 gate rejects at run). Needed: parse/ir/codegen/runner support.
  - `variable-aggregator` (first-non-null), `document-extractor` (str/list→text) — pure compute, direct codegen.
  - `loop` — container subgraph, reuse the `iteration` lower/codegen skeleton + break-condition eval.
  - `variable-assigner` (v2 items) — writes conversation/loop variables; **no session in single-shot execution**
    → decide between a same-run variable pool vs. keeping it Unsupported with a clear "conversation variables
    unsupported" error.
  - `http-request` — **security-sensitive, default off** (`RAGSPINE_DIFY_HTTP_ENABLED=false`): L0 gate rejects
    when disabled; when enabled, the runner injects a controlled urllib client (forced timeout ≤30s, http/https
    only, no non-http redirect, 1MB body cap) — **generated code never imports a network module** (import
    whitelist stays zero-widened).
- **P1 — richer Dify node forms.** LLM vision/memory/jinja2 editing; question-classifier `node_type` round-trips
  as `if-else` (IR de-Difyization) — add a source-type field if the console needs to distinguish.
- **P1 — Studio bundle split.** 638 KB single chunk → code-split React Flow / the workflow model layer.
- **P2 — API clone depth.** Dify token accounting (`total_tokens` currently 0); n8n `responseMode: onReceived`
  (webhook returns immediately, executes async); pin the cloned shapes against the upstream OpenAPI specs in CI.

## Out of scope

- **Reimplementing Dify/n8n execution semantics.** We clone the *ingress* (format + API shape); execution is
  always our deterministic gated engine, so nodes with no safe single-shot semantics (conversation state,
  arbitrary network egress by default) are explicitly gated or unsupported, not faked.
- **A hosted workflow registry / marketplace.** Workflows live in the browser `localStorage` (Studio) or the
  file-backed n8n store; there is no multi-tenant catalog service.
- **Auth on the compatibility endpoints beyond the platform's own scheme.** MVP mirrors ADR 0014's no-auth
  stance for `/v1/*` and uses the platform key for `/api/v1/*`; production still fronts with a reverse proxy /
  ingress auth (flagged in deploy docs).
