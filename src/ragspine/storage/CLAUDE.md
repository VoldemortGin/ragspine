---
covers:
  - src/ragspine/storage/
verified-against: 2d93b88
---

# storage — agent contract

Auto-loaded when working under `src/ragspine/storage/`. Keep terse; deep dives go in
`src/ragspine/storage/docs/`.

## What lives here

Fact store (numeric) + chunk store (narrative), sqlite-backed, full lineage.

## Invariants

- **Provenance** — every stored fact/chunk carries `source_doc_id` + locator;
  never drop lineage. Human corrections add `corrected_by` + `corrected_audit_seq` (the
  resolving SME and the review-audit `seq` behind the value) so a corrected fact stays
  traceable to who changed it and why; the applier keys idempotency on that `seq`.
- **`dim_key` is the upsert conflict key** — a canonical sorted-JSON natural key over
  the *identity* dims only (`metric`, `entity`, `channel`, `period=period_type+period`;
  geography is `identity=False`, an overwritable non-key column). It is computed from the
  typed columns (`_compute_dim_key`), is `UNIQUE`, and is **storage-only** — never a
  `Fact` field, recomputed on write and on legacy backfill, never reconstructed into
  `Fact(**data)`. The old composite `ux_fact_metric` index is kept alongside; both encode
  identical finance uniqueness. Keeping it 0-or-1-row is what preserves the deterministic
  found/not_found read path.

## Read before editing

- **`Fact`'s first ten fields are positional-frozen** — `metric_code, entity, geography,
  channel, period_type, period, value, unit, source_doc_id, source_locator`. `qa_eval`
  binds a 10-tuple via `Fact(*row)`; reordering or removing any breaks it. New fields are
  **additive only**, appended at the end (the arbitrary-dimension `dimensions` bag is the
  last field).
- **`dimensions` is an in-memory bag, excluded from DB columns**, reserved-name-guarded in
  `__post_init__` (it may never shadow a structural/lineage/`dim_key` column); empty bags
  derive an identity mirror. Don't write it to a column or let a reserved name through.

## Deep dives

<!-- none yet -->
