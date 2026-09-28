---
covers:
  - src/ragspine/common/
verified-against: 0dd6fefc8680435f27e8453aa1e7522d828dd34f
---

# common — agent contract

Auto-loaded when working under `src/ragspine/common/`. Keep terse; deep dives go in
`src/ragspine/common/docs/`.

## What lives here

Cross-cutting primitives: company profile, sensitivity model, glossary, observability,
global constants (`core` — data dir + default sqlite paths; single source of truth).

`observability/` is a package: `trace.py` (the `emit_trace` / `new_request_id` primitives —
behavior unchanged) + `sink.py` (the **`TraceSink` seam** — `make_trace_sink` /
`RAGSPINE_TRACE_SINK` registry + `ragspine.trace_sinks` entry-point discovery + the reusable
`enforce_trace_privacy` gate; **reuses** corespine's `@runtime_checkable TraceSink` Protocol +
`InProcessPrivacyTraceSink` default, no duplicate Protocol) + `adapters/otel.py` (`OtelTraceSink`,
behind `[otel]`, privacy-gated before any span) + `llm_calls.py` (per-call, per-stage LLM telemetry, ADR 0028,
stdlib only: `record_llm_calls` bucket, `llm_stage` closed-enum label — unknown ⇒ `ValueError` at definition time,
`instrument_llm_call` for provider `chat`, `note_attempt` / `note_truncated` / `note_reasoning_disabled`,
`llm_trace_fields`; entries are frozen `LLMCall`s of ints / bools / `None` / enum values; the collector never raises
and, with no bucket, returns the same object / re-raises the same exception; appends are locked, and an in-request
thread pool must use `copy_context().run` per task).

`answer_text.py` — `normalize_answer` / `contains_normalized`: number-friendly text normalization
(NFKC, thousands, `per cent`→`%`, magnitude amounts) + digit-boundary containment. One definition shared by the
nl-gold judge (`eval/`, re-exported with unchanged signatures) and the narrative number guard (`agent/number_guard`,
ADR 0024).

`llm_json.py` — `extract_json(text, *, expect=None)`: the one tolerant parser for JSON in LLM replies. A whole text
that strictly parses to the expected container is returned as is; otherwise strict parse per candidate (fences
first, then the whole text), then only these repairs, outside string literals: strip code fences, drop a comma right before `]` / `}`,
quote a bare ASCII identifier in value position (only when the segment already has a double-quoted string; never
`true/false/null/None/NaN/Infinity/undefined`), take the first top-level container out of surrounding prose.
Returns dict / list / `None`, never raises (RecursionError included), never looks inside a top-level span, tries
at most 64 starts. No truncation completion, single quotes, unquoted keys, comments, Python literals or `NaN`.
stdlib only, no trace. Known limit: with prose around the JSON, ``` inside a JSON string can still be taken as a
fence. Call sites use `parse_llm_json`: the original `json.loads(text.strip())` first, returned unchanged (any type,
`NaN` / `Infinity` included), and `extract_json` only on failure — replies that parsed before behave exactly as
before. Used by `agent/decompose`, `agent/query_transform` (RAG-Fusion) and `graph/extractor`.

`evidence/` — the evidence chain's APP_* settings, lineage logging and model access
(ADR 0022); its own contract is [`evidence/CLAUDE.md`](evidence/CLAUDE.md).

## Invariants

- **Privacy-aware traces** — `observability` records codes / counts / timings
  only, never answer / fact value / chunk text. **Mechanically enforced**: `emit_trace`
  runs every payload through `enforce_trace_privacy` and a corespine `InProcessPrivacyTraceSink` first — a
  forbidden content key (answer/value/text/content/prompt/completion/chunk/chunk_text/body) raises
  `TraceError` before anything is logged. The ragspine gate is **recursive** (ADR 0028): Mapping keys and
  list/tuple elements at any depth, error with a path like `llm_calls[0].prompt`; more than `MAX_TRACE_DEPTH = 8`
  container levels is rejected as suspicious, never truncated. corespine's sink itself still checks only the top
  level (recursive corespine check is a follow-up). Privacy by construction, not by convention.
  **Formalized as a seam** (`sink.py`): any fan-out sink (incl. `OtelTraceSink`) calls
  `enforce_trace_privacy` first, so it goes *through* the same privacy gate, never around it (the built-in
  `in_process` sink is a thin corespine subclass that does) — bound for every registered sink by
  `tests/conformance/test_trace_sink.py` (nested-leak payloads included, + three reverse-proof stubs that must
  FAIL: verbatim leak, value smuggling, top-level-only gate). `make_trace_sink()` defaults to `None` ⇒
  `emit_trace` path byte-identical.
- **Config-driven** — identity / metrics / competitors come from `CompanyProfile`;
  never hardcode a company.

## Read before editing

- **The `TraceSink` seam reuses corespine, it does not re-define.** The Protocol / default /
  `FORBIDDEN_KEYS` / `TraceError` all come from corespine; `sink.py` adds only the ragspine-side
  registry + privacy gate + adapters. Don't fork a second Protocol. Any new sink **must** route
  through `enforce_trace_privacy` (or reject forbidden keys itself) or it fails the conformance pack.
- **Default `emit_trace` stays byte-identical.** The seam is formalization + opt-in registration;
  wiring `emit_trace` to a config-selected sink for live multi-exit fan-out is a follow-up.

## Deep dives

<!-- none yet -->
- The `TraceSink` seam contract lives inline above + in `docs/prd-breadth-via-adapters.md`
  (Trace sink row) + `docs/invariants.md` (Privacy-aware traces). No separate deep dive yet.
