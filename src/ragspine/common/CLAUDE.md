---
covers:
  - src/ragspine/common/
verified-against: 795ed14
---

# common — agent contract

Auto-loaded when working under `src/ragspine/common/`. Keep terse; deep dives go in
`src/ragspine/common/docs/`.

## What lives here

Cross-cutting primitives: company profile, sensitivity model, glossary, observability,
global constants (`core` — data dir + default sqlite paths; single source of truth).

## Invariants

- **Privacy-aware traces** — `observability` records codes / counts / timings
  only, never answer / fact value / chunk text.
- **Config-driven** — identity / metrics / competitors come from `CompanyProfile`;
  never hardcode a company.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
