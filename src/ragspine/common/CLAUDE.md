---
covers:
  - src/ragspine/common/
verified-against: d474ea9
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
behind `[otel]`, privacy-gated before any span).

## Invariants

- **Privacy-aware traces** — `observability` records codes / counts / timings
  only, never answer / fact value / chunk text. **Mechanically enforced**: `emit_trace`
  runs every payload through a corespine `InProcessPrivacyTraceSink` first — a forbidden
  content key (answer/value/text/content/prompt/completion/chunk/chunk_text/body) raises
  `TraceError` before anything is logged. Privacy by construction, not by convention.
  **Formalized as a seam** (`sink.py`): any fan-out sink (incl. `OtelTraceSink`) calls
  `enforce_trace_privacy` first, so it goes *through* the same privacy gate, never around it —
  bound for every registered sink by `tests/conformance/test_trace_sink.py` (+ two content-leaking
  reverse-proof stubs that must FAIL). `make_trace_sink()` defaults to `None` ⇒ `emit_trace` path
  byte-identical.
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
