---
covers:
  - src/ragspine/ingestion/
verified-against: 2d93b88
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
- **Review write-back closes the loop** — `review/apply.py` `ResolvedReviewApplier`
  applies a resolved review item back to the `FactStore` so SME decisions actually change
  what `query()` returns: **approve → visible** (`set_review_status` → `APPROVED`),
  **reject → invisible** (`→ REJECTED`), **reject + corrected_value → a corrected, visible
  fact** (re-upsert with the new value, `APPROVED`, stamped `corrected_by` /
  `corrected_audit_seq`, source lineage preserved). Idempotent by audit `seq`
  (re-applying is a `noop`). `enqueue_fact_for_review(...)` is the on-ramp (writes the fact
  `PENDING` → invisible, enqueues `payload={"fact": …}`); the default ingestion path stays
  `auto_approved`. The applier emits no value/answer trace (privacy-aware). The RQ worker
  job (`run_apply_review_job`) is a deferred follow-up.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
