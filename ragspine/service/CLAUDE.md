---
covers:
  - ragspine/service/
verified-against: 3112e68
---

# service — agent contract

Auto-loaded when working under `ragspine/service/`. Keep terse; deep dives go in
`ragspine/service/docs/`.

## What lives here

`ServiceConfig` (env `RAGSPINE_*`), FastAPI app (app factory + dependency
injection), RQ task queue (`FakeQueue` tests / `RQQueue` prod), ingestion jobs
(worker-owned stores), FAQ short-circuit cache.

## Invariants

- **FAQ conservative exclusions** — structured-numeric / competitor / real-time /
  expired / disabled / RESTRICTED content must never short-circuit. The FAQ layer
  sits in front of the anti-fabrication guard, so a wrong short-circuit bypasses it.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
