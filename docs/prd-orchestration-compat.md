# PRD — Orchestration Compatibility: Dify / n8n ingress, the Studio console & external-API shape cloning

> **status:** in progress (Dify compiler+runner, per-node trace, n8n↔Dify conversion, Studio editor,
> and Dify/n8n **API shape clones** are implemented; the 1,000-template catalog and preview v1 are implemented
> in the 0.11 source worktree but not yet released; installed-package local run/serve/open remains incomplete)
> · **created:** 2026-07-12 · **last audited:** 2026-07-17
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

Status is audited against code, focused tests, and the installed PyPI artifact:

- `[x]` — implemented, tested, and reachable through the stated surface.
- `[~]` — partially delivered; the row states the missing user or release boundary.
- `[ ]` — not implemented.

| Surface | Piece | Status | Where |
|---|---|---|---|
| **Dify format** | YAML/JSON/TOML → pure-Python compiler + static analyzer | [x] | `src/ragspine/dify/` + `workflows/formats.py` (ADR 0013) |
| | analyze / compile / run + async job endpoints | [x] | `service/api/routes.py` (ADR 0014) |
| | safe execution L0 static gate / L1 sandbox / L2 subprocess in the source tree | [x] | `service/dify/{safety,runner}.py` |
| | L2 subprocess from an installed wheel | [~] | runner resolves `scripts/run_dify_workflow.py`, but the wheel does not currently include that repository script |
| | **per-node execution trace** (status/timing/IO, privacy-gated) | [x] | `dify/codegen/emitter.py` + `service/dify/tracing.py` |
| **n8n format** | n8n JSON ↔ Dify DSL bidirectional lossless conversion | [x] | `src/ragspine/n8n/` |
| | `POST /v1/n8n/convert` · `POST /v1/n8n/run` | [x] | `service/api/routes.py` |
| **Dify API clone** | `POST /v1/workflows/run` (blocking + SSE streaming) | [x] | `service/api/dify_public.py` |
| | `GET /v1/workflows/run/{id}` · `/v1/info` · `/v1/parameters` | [x] | `service/api/dify_public.py` |
| | Bearer app-key registry (`RAGSPINE_DIFY_PUBLIC_APPS`) | [x] | `service/config.py` |
| **n8n API clone** | `/api/v1/workflows` CRUD + activate/deactivate | [x] | `service/n8n_public/` |
| | `/api/v1/executions` · `X-N8N-API-KEY` auth · cursor paging | [x] | `service/n8n_public/` |
| | `POST /webhook/{path}` triggers active workflow → convert → run | [x] | `service/n8n_public/router.py` |
| **Workflow catalog / local DX** | bounded JSON/YAML/TOML input normalization | [x] | `workflows/formats.py`; CLI analyze/compile accept all four file suffixes |
| | natural-language/explicit-template `workflow create`, plus `list` and `show` | [x] | PyPI 0.10 exposes the commands; YAML/JSON output is tested |
| | 1,000-template generated catalog, integrity/source policy, semantic matching | [~] | implemented and tested in the 0.11 worktree; PyPI 0.10 still contains 7 templates |
| | `workflow preview <template-id>` graph-only preview v1 | [~] | implemented in the 0.11 worktree; not on PyPI and does not accept a local file |
| | catalog/detail/scaffold HTTP APIs with preview v1 | [~] | implemented and tested in the 0.11 worktree; release pending |
| | static website export and browser graph preview for 1,000 templates | [x] | `scripts/export_workflow_catalog.py`; deployed at `rag-spine.org/workflows` |
| | preview an arbitrary local JSON/YAML/TOML workflow | [ ] | no CLI path; `preview` accepts only a catalog ID |
| | one-shot `workflow run <file>` with inputs and node traces | [ ] | no CLI subcommand; only the HTTP execution surface exists |
| | `workflow serve <file> --open`: start local service, open Studio, auto-load file | [ ] | no serve/open command and no Studio bootstrap/deep-link contract |
| **Studio console** | React Flow editor: Dify workflow ⇄ canvas lossless round-trip | [x] | `studio/` |
| | execution visualization (node badges/timing/edge highlight/IO) | [x] | `studio/src/pages/workflows/` |
| | undo/redo · copy-paste · multi-select · inline `{{#var#}}` autocomplete | [x] | `studio/` |
| | drag-to-add node picker · canvas search · snap grid · templates | [x] | `studio/` |
| | JSON/YAML/TOML import; Dify YAML and n8n JSON export | [x] | `studio/src/pages/workflows/modals/` |
| | installed-package delivery and one-command bootstrap | [~] | service can mount a prebuilt `RAGSPINE_STUDIO_DIR`; `studio/dist` is not shipped in the wheel |
| | automatically load the CLI-selected file without exposing its path/content in the URL | [ ] | App starts from browser `localStorage`; no launch-session or deep-link ingress |
| **Extended nodes** | 5 nodes (http-request, variable-aggregator/-assigner, document-extractor, loop) — **canvas + forms + variable inference** | [x] | `studio/src/workflow/registry.ts` + `forms/` |
| | same 5 nodes — **compile/execute (parse/ir/codegen/runner)** | [ ] | `src/ragspine/dify/` — **next** |
| **Release** | 0.11 source version, tests, README, catalog and preview implementation | [~] | present in source; no tag or distribution artifact |
| | PyPI `rag-spine==0.11.0` and clean-install smoke | [ ] | PyPI latest is 0.10.0 as of 2026-07-17 |

## What is implemented this cycle (2026-07)

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
4. **Studio console** — the full visual editor (see matrix), 232 vitest + `tsc --noEmit` green, mounted at
   `/studio` by the service.
5. **Workflow discovery source implementation (release pending)** — bounded JSON/YAML/TOML input, natural-
   language and explicit-template scaffolding, the generated 1,000-template catalog, graph-only preview v1,
   HTTP catalog/scaffold responses, and the static website export contract are implemented and focused tests
   are green. This is not called shipped until the 0.11 wheel passes a clean-install smoke and reaches PyPI.

## Remaining backlog / next steps

- **P0 — installed local-workflow loop.** Complete the user journey without requiring a source checkout,
  Node/pnpm, environment-variable assembly, manual YAML import, or a second terminal.
  - `ragspine workflow preview <file-or-template-id>` accepts JSON/YAML/TOML files and catalog IDs, normalizes
    both through the same preview v1 projection, and remains display-only.
  - `ragspine workflow run <file> --inputs <json>` reuses the existing compiler, L0/L1/L2 runner, trace shape,
    timeouts, provider injection and `RAGSPINE_DIFY_RUN_ENABLED` policy; it does not add a second executor.
  - `ragspine workflow serve <file-or-template-id> --open` binds to `127.0.0.1` by default, selects or reports
    the port deterministically, starts the API + packaged Studio, opens the browser exactly once through
    Python's cross-platform browser API, and automatically loads the selected document.
  - Auto-load uses an opaque, bounded launch-session identifier; workflow contents, filesystem paths and
    credentials never enter a query string. Opening a graph does not silently enable execution.
  - Package the prebuilt Studio assets in the wheel (or provide an equally deterministic artifact lookup);
    macOS, Windows and Linux clean-install tests exercise paths containing spaces and non-ASCII characters.
- **P0 — publish and prove 0.11.** Commit the reviewed source, run the full gate, build sdist/wheel, run a fresh
  `uv`-environment smoke for `create/list/show/preview`, verify the 1,000-template count, exercise Studio launch,
  and prove L2 subprocess execution from the wheel. Publish only after those installed-artifact checks pass;
  then verify PyPI metadata and mark the release rows `[x]`.
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

## Audit summary (2026-07-17)

| Area | Done | Partial | Pending |
|---|---:|---:|---:|
| Dify compiler / API clone | 7 | 1 | 0 |
| n8n format / API clone | 5 | 0 | 0 |
| Workflow catalog / local DX | 3 | 3 | 3 |
| Studio console | 5 | 1 | 1 |
| Extended nodes | 1 | 0 | 1 |
| Release | 0 | 1 | 1 |
| **Total** | **21** | **6** | **6** |

## Out of scope

- **Reimplementing Dify/n8n execution semantics.** We clone the *ingress* (format + API shape); execution is
  always our deterministic gated engine, so nodes with no safe single-shot semantics (conversation state,
  arbitrary network egress by default) are explicitly gated or unsupported, not faked.
- **A hosted workflow registry / marketplace.** Workflows live in the browser `localStorage` (Studio) or the
  file-backed n8n store; there is no multi-tenant catalog service.
- **Auth on the compatibility endpoints beyond the platform's own scheme.** MVP mirrors ADR 0014's no-auth
  stance for `/v1/*` and uses the platform key for `/api/v1/*`; production still fronts with a reverse proxy /
  ingress auth (flagged in deploy docs).
