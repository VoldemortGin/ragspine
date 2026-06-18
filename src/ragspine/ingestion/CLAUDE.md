---
covers:
  - src/ragspine/ingestion/
verified-against: cab40fe
---

# ingestion — agent contract

Auto-loaded when working under `src/ragspine/ingestion/`. Keep terse; deep dives go
in `src/ragspine/ingestion/docs/`.

## What lives here

IR/text → stores. `structured/` (fact ingestion + idempotent batch manifest
ledger), `narrative/` (document chunk ingestion + extraction), `review/` (SME
human review-queue state machine).

## Invariants

- **Idempotent structured ingestion** — re-running a batch must not double-write;
  the manifest ledger is the guard.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
