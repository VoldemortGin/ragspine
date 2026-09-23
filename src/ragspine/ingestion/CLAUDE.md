---
covers:
  - src/ragspine/ingestion/
verified-against: 4846bb2ded70e5f485566da9a0edf30c446fdf39
---

# ingestion — agent contract

Auto-loaded when working under `src/ragspine/ingestion/`. Keep terse; deep dives go
in `src/ragspine/ingestion/docs/`.

## What lives here

IR/text → stores. `source/` (the `SourceConnector` seam — *where* raw documents enter
ingestion: a `Protocol` + frozen `RawDoc` + a dependency-free `FilesystemConnector` / `InMemoryConnector`
default + lazy-`httpx` `HttpConnector` / `NotionConnector` (behind `[connectors]`) +
`make_source_connector` / `RAGSPINE_SOURCE_CONNECTOR` config selector with entry-point discovery + a
`bridge.ingest_from_connector` that carries `RawDoc` lineage end-to-end into the `FactStore`),
`structured/` (fact ingestion + idempotent batch manifest ledger), `narrative/` (document chunk
ingestion + extraction; sources: `.pptx` / `.pdf` / `.docx` / `.docm` + `.txt` plain text + `.md`
DI markdown (`extract_di_markdown_narrative` over `extraction/di_markdown`: one segment per (page, heading
path), locator `page={DiPage.index}` = physical page order, tables linearized to `row | col header: value`
lines, figure caption first; plain markdown = one page) —
`ingest_narrative(..., chunker=, max_chars=, overlap_chars=)` routes chunking through the retrieval
`Chunker` seam — size/overlap pass through to it, defaults `DEFAULT_CHUNK_CHARS` / `DEFAULT_OVERLAP_CHARS`;
default `chunker=None` → built-in `chunk_document` **byte-identical**; injecting `make_chunker("parent_child")` lands children with
`window_text` / `parent_locator` that `ChunkStore` now persists for store-level small-to-big, ADR 0018;
`segment_chunking=True` (default `False` = old whole-document chunking, **byte-identical**, frozen by
`tests/ingestion/narrative/test_legacy_chunk_snapshot.py`) chunks each segment separately via `chunk_segments`:
locator `{doc_id}@{segment locator}#para…` (e.g. `deck.md@page=3#para1-4`), `heading` = heading path joined
by `" > "`, seq/chunk_id renumbered doc-wide; `.md` (`SEGMENT_CHUNKED_SUFFIXES`) always takes this path),
`review/` (SME human review-queue state machine).

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

- **`ingest_file` carries opt-in extractor seams; defaults are byte-identical.** `.pdf` accepts
  `grid_extractor` (digital) + `ocr_backend` (scanned); `.pptx` accepts `pptx_extractor` (W3c) — inject
  a family `PptspineGridExtractor` for richer table merges, else the default stays python-pptx
  (`pptx_styled@1`, which keeps colour/chart/note). The injected extractor's `version` is stamped into
  fact lineage. Adding a seam must not change the no-injection path.

## Deep dives

- [`docs/source-connector.md`](docs/source-connector.md) — the `SourceConnector` seam: the `Protocol`,
  the frozen `RawDoc`, the `FilesystemConnector` offline default (deterministic walk, `source_doc_id =
  path.name`), the `make_source_connector` factory + entry-point discovery, and the provenance
  conformance pack bound at the point of entry. Shipped seam-first (not yet wired into narrative ingest).
