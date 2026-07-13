---
covers:
  - src/ragspine/storage/
verified-against: 006a655
---

# storage — agent contract

Auto-loaded when working under `src/ragspine/storage/`. Keep terse; deep dives go in
`src/ragspine/storage/docs/`.

## What lives here

The **`FactStore` seam** (`fact_store.py`) — the structured numeric channel, sqlite-backed, full lineage.
Formalized into the five-part breadth contract (same paradigm as `make_vector_store` / `make_graph_store` /
`make_trace_sink`), because the structured store is the **anti-fabrication invariant's storage root**:

- **Protocol** — a `@runtime_checkable FactStore` `Protocol` lifting the sqlite store's public interface
  (`query`, `upsert_facts`, `delete_by_source_doc`, `get_by_dim_key`, `set_review_status`, `dim_key_for`,
  `init_schema`, `count`, `close`, `execute_read`). Core imports only this Protocol.
- **Offline default** — `SqliteFactStore` (the prior concrete `FactStore`, **renamed**; stdlib sqlite3, zero
  third-party deps, **behavior byte-identical** — pure structural extraction).
- **Registry** — `make_fact_store` / `RAGSPINE_FACT_STORE` (built-in `sqlite` default + `ragspine.fact_stores`
  entry-point auto-discovery). **Default spec → sqlite default**, so the structured loop stays byte-identical.
- **Conformance** — `tests/conformance/test_fact_store.py` binds anti-fabrication + provenance for every
  registered impl (found-determinism / miss→empty / lineage survival; two reverse-proof stubs that must FAIL).
- **Adapters** — DuckDB / Postgres are a follow-up (need external deps; behind their own extras / third-party
  entry-point registration — deliberately not pulled into CI).

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

- **`FactStore` is now the Protocol, not a class you instantiate.** Construct the default via
  `SqliteFactStore(db_path)` or `make_fact_store(db_path=…)` — `FactStore(…)` raises (Protocols can't be
  instantiated). Use `FactStore` only as a type hint / `isinstance` target. The Protocol **mirrors the existing
  public interface** (structural extraction — do not add/remove methods or change a signature, or you change the
  contract). `execute_read` is a sqlite-specific raw-read escape hatch kept on the public face for byte-identity
  (observation surfaces depend on it); a non-sqlite adapter returns a compatible row-like or those callers take
  the concrete — a DuckDB/Postgres follow-up concern, outside the anti-fabrication/provenance core.
- **`Fact`'s first ten fields are positional-frozen** — `metric_code, entity, geography,
  channel, period_type, period, value, unit, source_doc_id, source_locator`. `qa_eval`
  binds a 10-tuple via `Fact(*row)`; reordering or removing any breaks it. New fields are
  **additive only**, appended at the end (the arbitrary-dimension `dimensions` bag is the
  last field). For hand-written call sites, prefer the keyword-only builder
  **`Fact.metric(...)`** (order-immune; `channel` defaults `"TOTAL"`, `geography` defaults
  `""`, v2 fields pass through) over the positional constructor — it can't silently misorder.
- **`dimensions` is an in-memory bag, excluded from DB columns**, reserved-name-guarded in
  `__post_init__` (it may never shadow a structural/lineage/`dim_key` column); empty bags
  derive an identity mirror. Don't write it to a column or let a reserved name through.
- **`SqliteFactStore` is single-threaded — one instance per thread/request.** It holds one
  `sqlite3` connection bound to its creating thread (`check_same_thread=True`); using an
  instance from another thread raises. Under a FastAPI threadpool, open one store per
  request/op (the service's `open_fact_store(config)` context manager already does), never
  share one across requests. True concurrent read/write (connection pool / WAL) is a
  deliberate follow-up — this default stays zero-dep single-connection. `has_source_doc(id)`
  is the cheap existence probe (does any fact for that `source_doc_id` exist, regardless of
  `review_status`).

## Deep dives

<!-- none yet -->
