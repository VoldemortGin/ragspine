---
covers:
  - src/ragspine/ingestion/
verified-against: 8b6b4d6
---

# ingestion — agent contract

Auto-loaded when working under `src/ragspine/ingestion/`. Keep terse; deep dives go
in `src/ragspine/ingestion/docs/`.

## What lives here

IR/text → stores. `source/` (the `SourceConnector` seam — *where* raw documents enter
ingestion: a `Protocol` + frozen `RawDoc` + a dependency-free `FilesystemConnector` default +
`make_source_connector` / `RAGSPINE_SOURCE_CONNECTOR` config selector with entry-point discovery),
`structured/` (fact ingestion + idempotent batch manifest ledger), `narrative/` (document chunk
ingestion + extraction), `review/` (SME human review-queue state machine).

## Invariants

- **Provenance at the point of entry** — every `RawDoc` a `SourceConnector` yields carries a non-null
  `source_doc_id` (= filename, the lineage root — same as `narrative_ingest`'s `doc_id = path.name`)
  + `locator`. Bound for *every* registered connector by `tests/conformance/test_source_connector_provenance.py`
  (with a lineage-dropping reverse-proof stub). A connector that drops lineage fails CI, not production.
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

- [`docs/source-connector.md`](docs/source-connector.md) — the `SourceConnector` seam: the `Protocol`,
  the frozen `RawDoc`, the `FilesystemConnector` offline default (deterministic walk, `source_doc_id =
  path.name`), the `make_source_connector` factory + entry-point discovery, and the provenance
  conformance pack bound at the point of entry. Shipped seam-first (not yet wired into narrative ingest).
