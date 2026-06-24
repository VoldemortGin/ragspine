---
covers:
  - src/ragspine/common/
verified-against: 92022e0
---

# common — agent contract

Auto-loaded when working under `src/ragspine/common/`. Keep terse; deep dives go in
`src/ragspine/common/docs/`.

## What lives here

Cross-cutting primitives: company profile, sensitivity model, glossary, observability,
global constants (`core` — data dir + default sqlite paths; single source of truth).

## Invariants

- **Privacy-aware traces** — `observability` records codes / counts / timings
  only, never answer / fact value / chunk text. **Mechanically enforced**: `emit_trace`
  runs every payload through a corespine `InProcessPrivacyTraceSink` first — a forbidden
  content key (answer/value/text/content/prompt/completion/chunk/chunk_text/body) raises
  `TraceError` before anything is logged. Privacy by construction, not by convention.
- **Config-driven** — identity / metrics / competitors come from `CompanyProfile`;
  never hardcode a company.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
