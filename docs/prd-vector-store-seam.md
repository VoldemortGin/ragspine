# PRD — VectorStore seam: pluggable vector index + filtered ANN

> **status:** implemented (seam + wiring + persistence + 2 adapters; more adapters open) · **created:** 2026-06-17 · **methodology:** TDD (red conformance tests first)
> Originating spec, retained for history — the live contract is now
> [`src/ragspine/retrieval/docs/vector-store.md`](../src/ragspine/retrieval/docs/vector-store.md)
> (with `covers:`), so this PRD carries none.
> Lands the **P0 seam** of [`prd-breadth-via-adapters.md`](prd-breadth-via-adapters.md).
> **What's done vs. what's next is consolidated in [Status — shipped & remaining](#status--shipped--remaining-updated-2026-06-19) below;** the sections after it are the originating spec, retained as written.

## Status — shipped & remaining (updated 2026-06-19)

This PRD's vision is now **substantially shipped, across four increments**; the live, drift-tracked
contract is [`vector-store.md`](../src/ragspine/retrieval/docs/vector-store.md). What's done and what's next:

### ✅ Shipped

1. **The seam.** `VectorStore` Protocol (`upsert` / `query+where` / `delete` / `count`) +
   `InProcessVectorStore` (zero-dependency deterministic default) + the invariant-binding **conformance
   kit** (`tests/conformance/`), parametrized over *every* registered store — provenance / isolation /
   determinism bound at the seam, so a backend that breaks the spine fails CI, not production.
2. **Live wiring.** `HybridRetriever` delegates vector scoring to the seam **byte-identically** (a captured
   golden pins the `(bm25, vector, fused)` triples); `make_vector_store(spec)` / `RAGSPINE_VECTOR_STORE`
   config selector; threaded through `NarrativeIndex` / `build_narrative_retriever` / `ServiceConfig`; the
   resolved store is **named in `.topology()`**.
3. **Sensitivity-gated persistence.** `NarrativeIndex` **embeds-and-persists at ingest**, invalidates by
   `doc_id` (not blast-all), and retrieves **store-managed** (`HybridRetriever(manage_vectors=False)`) so a
   fresh process re-uses persisted vectors with **zero chunk re-embedding**. The swappable
   **`PersistencePolicy`** seam (one method, `persistable(chunk)`) gates what is written at rest — default
   `IsolationFirstPolicy` **never persists a `RESTRICTED` chunk's vector**; `PersistEverythingPolicy` is
   opt-in for a db classified RESTRICTED-tier (`docs/invariants.md`).
4. **Two real adapters** (behind `[vector]`, each inheriting the whole conformance kit):
   - **#1 `SqliteVecVectorStore`** — embedded (sqlite-vec `vec0`), persistent; conformance gates
     **unconditionally** (sqlite-vec is in the dev install).
   - **#2 `PgVectorVectorStore`** — networked Postgres/pgvector via the **`pg8000` (BSD)** driver — *not*
     psycopg/LGPL, per ADR 0009's ≤ Apache-2.0 gate. Conformance binds against a `RAGSPINE_PG_URL`
     Postgres and **skips** in the default no-server CI (a server backend can't be required of every
     contributor); verified green against local Postgres 17 + pgvector 0.8.0.
5. **Tests:** **1195 passed** (default) / **1232 with a pgvector Postgres**, 1 gpu-skipped; conformance
   runs over `in_process` + `sqlite_vec` (+ `pgvector` when `RAGSPINE_PG_URL` is set).

### ⏳ Remaining (roadmap)

- **More adapters** — **Qdrant** (next), then Milvus, FAISS (see the [adapter roadmap](#adapter-roadmap-approved-priority--license-tiering) table; each is one registration line + the inherited conformance kit).
- **Native ANN / KNN** — both shipped adapters persist but currently **score exactly in Python**
  (sqlite-vec full-scans; pgvector pushes the `where` to SQL but re-scores in Python). Native indexed KNN
  (sqlite-vec `MATCH`; pgvector HNSW / IVFFlat) with an **exact re-rank** is the scale optimization,
  deliberately out of the first adapters.
- **The exact-vs-approximate capability flag** in the conformance kit — *not yet implemented* because all
  shipped stores are **exact**; it lands with the first *approximate* (HNSW) backend so its weaker
  determinism guarantee doesn't falsely fail (see [Further notes](#further-notes)).
- **Entry-point auto-discovery** — backends are config-string-selectable today via `make_vector_store`;
  letting a third-party package register a backend by Python **entry point** (so `ragspine-qdrant` needs no
  core PR) is still open — tracked in the parent [breadth PRD](prd-breadth-via-adapters.md).

## Problem statement

RAGSpine has **no vector store and no persistent vector index**. Pluggability stops one layer too early:

- `EmbeddingBackend` (`retrieval/lexical/retrieval.py:46`) is a `Protocol` — text → vectors *is* swappable
  (`Deterministic` / `OpenAI` / `SentenceTransformer`, selectable by `make_embedding_backend(spec)`).
- But the **vectors themselves are never stored**. `ChunkStore` (sqlite, `chunking/chunk_store.py`) persists
  chunk *text + metadata*; its table has no vector column.
- Vectors are computed **lazily at query time** inside `HybridRetriever.search` (`retrieval.py:258-264`),
  cached in an **in-process dict** `_embedding_cache` (`retrieval.py:212`) that is **cleared on every
  `ingest()`** (`retrieval.py:340`).
- Similarity search is a **pure-Python brute-force cosine scan** over every metadata-pre-filtered candidate
  (`retrieval.py:279-288`, `cosine_similarity` at `retrieval.py:103`) — no ANN, no persistence.

This is clean and correct for the offline-deterministic lean core (ADR 0005), but it does not scale (O(N)
cosine recomputed per process), it cannot persist an index, and — critically for the breadth strategy — there
is **no seam** at which a Qdrant / pgvector / FAISS backend could be plugged in, nor where filter-pushdown
(the mechanism that carries the isolation invariant) can live in the store itself.

## Solution

Introduce a `VectorStore` `Protocol` that owns exactly one concern: **store vectors + answer a filtered
top-k similarity query**. It does *not* own BM25, RRF fusion, or reranking — those stay in `HybridRetriever`.

- `VectorStore` `Protocol` — `upsert` / `query(vector, k, where)` / `delete(where)` / `count`.
- `InProcessVectorStore` — a dependency-free, deterministic default whose behavior is **equivalent to the
  cosine loop in `HybridRetriever` today** (brute-force cosine + id-ascending tie-break). Keeps the
  `pip install ragspine` lean default runnable end-to-end with zero extras (ADR 0009).
- A **conformance suite** (`tests/conformance/`) that every implementation — the in-process default now, a
  Qdrant/pgvector adapter later — runs through, binding **provenance / isolation / determinism** at the seam.
- Real adapters (Milvus, pgvector, Qdrant, FAISS) are **out of scope here**; they land later behind a
  `[vector]` extra, **selectable purely by config** (`vector_store.backend = "milvus" | "pgvector" | …`,
  mirroring the existing `make_embedding_backend(spec)` / `RAGSPINE_*` factory idiom), each registering into
  the conformance parametrization and inheriting the whole suite. Swapping backend = changing config; the
  conformance kit is what makes that swap *safe* (a mis-mapped filter or dropped lineage fails CI).

## Proposed API surface

```python
# src/ragspine/retrieval/vector/store.py
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

DEFAULT_QUERY_K = 50      # mirrors retrieval.DEFAULT_TOP_K

@dataclass(frozen=True)
class VectorRecord:
    """A vector + its identity + filterable metadata. Provenance-carrying."""
    id: str                                  # chunk_id — the provenance anchor
    vector: tuple[float, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)   # doc_id, source_locator, topic, …, sensitivity

@dataclass(frozen=True)
class VectorHit:
    """A search result: id + cosine score + metadata (lineage preserved)."""
    id: str
    score: float                             # cosine similarity; higher = nearer
    metadata: Mapping[str, str]

@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, records: Sequence[VectorRecord]) -> int: ...
    def query(self, vector: Sequence[float], *, k: int = DEFAULT_QUERY_K,
              where: Mapping[str, str] | None = None) -> list[VectorHit]: ...
    def delete(self, *, where: Mapping[str, str]) -> int: ...
    def count(self) -> int: ...

class InProcessVectorStore:                  # the dep-free, deterministic default
    ...
```

## Contract (binding — tests and impl must agree)

- **upsert** — writes records; an `id` already present is **replaced** (upsert, not append). Returns count
  written. Empty input → `0`, no error. All vectors in the store share **one dimension**; a record whose
  vector length differs (within a call or across calls) raises `ValueError`.
- **query** — returns the **top-k by descending cosine score**, regardless of score magnitude (thresholding
  is the retriever's job, not the store's). Ties broken by **`id` ascending** (deterministic). `k` caps the
  result; `k` larger than the store returns all. Query vector length must equal the stored dimension
  (`ValueError` otherwise); on an **empty store** → `[]` (no dimension to check). A zero vector yields
  cosine `0.0` everywhere — no crash (mirrors `cosine_similarity`).
- **where (filter pushdown)** — exact-match, **AND** over all given keys, applied **before scoring**. A
  record missing a filtered key is excluded. A record that fails the filter **never appears, even if it is
  the exact nearest neighbor**. Empty filtered set → `[]`.
- **delete** — removes every record matching `where` (same semantics as query's filter); returns count
  removed. Re-ingest idempotency = `delete(where={"doc_id": d})` then `upsert(...)`.
- **count** — number of stored records (post-upsert/delete).
- **provenance** — every `VectorHit` carries a non-empty `id` and round-trips the record's `metadata`
  (notably `doc_id` + `source_locator`); the store never invents or drops an id.
- **determinism** — identical upserts + identical query → byte-identical hits (ids *and* scores), across
  repeated calls and across two independent store instances.

## How it slots in (and what this PRD does *not* change)

`HybridRetriever` keeps orchestrating; the vector channel's inline cosine loop is replaced by a
`VectorStore.query(...)` call, BM25 stays as-is, RRF fuses the two rankings as today. The metadata
pre-filter (today on `ChunkStore.iter_chunks`) becomes the `where` argument pushed into the store.

**Scope guard (minimal diff):** v1 shipped the **standalone `VectorStore` component + its conformance
suite**; the **wiring follow-up has since landed** — `HybridRetriever` now delegates vector scoring to
the seam *byte-identically* (a captured golden pins the triples), config-selectable via
`make_vector_store` and reflected in `.topology()`. **Persisting vectors alongside `ChunkStore`
remains a separate, still-deferred step** (isolation reasons — see "Out of scope"). This mirrors how
`EmbeddingBackend` was added as a seam first, then wired.

## Adapter roadmap (approved priority + license tiering)

Adapters land in **this order** (decided 2026-06-17). Each is **config-selected**
(`vector_store.backend = "..."`), lazy-imported behind a `[vector]` extra, and **must pass the full
conformance suite before it ships**.

| # | Backend | Form | License | Tier | Filter pushdown | exact/approx | Status |
|---|---|---|---|---|---|---|---|
| 0 | `InProcessVectorStore` | in-process | (core) | **default** | Python | exact | ✅ shipped |
| 1 | **sqlite-vec** | embedded (sqlite ext) | Apache-2.0 / MIT | promote | Python `_matches` (full-scan) | exact | **✅ shipped** |
| 2 | **pgvector** | Postgres ext (pg8000/BSD) | PostgreSQL (permissive) | promote | SQL JSONB `WHERE` | exact (Python re-score) | **✅ shipped** |
| 3 | **Qdrant** | server (Rust) | Apache-2.0 | promote | native filter | approx (HNSW) | next |
| 4 | **Milvus** | server | Apache-2.0 | promote | native expr | approx | later |
| 5 | **FAISS / hnswlib** | in-process lib | MIT / Apache-2.0 | promote | none → wrap | flat=exact / HNSW=approx | later |

- **Why sqlite-vec is #1** — RAGSpine is already sqlite-native (`ChunkStore`, `FactStore`). It is the natural
  *in-process default → embedded persistence* step: same file-based, zero-server, permissive, cross-platform,
  minimal new operational surface.
- **License tiering (operationalizes ADR 0009).** *promote* = officially supported, fully permissive
  (≤ Apache-2.0). **SSPL / proprietary backends (MongoDB, classic Elasticsearch/Redis, Pinecone) are
  community-tier only**: a permissive *client SDK* passes the dependency gate, but they are not officially
  promoted — RAGSpine's compliance-minded positioning leads with fully-permissive backends. (ES/Redis added
  AGPLv3 in 2024–25; AGPL is still outside the ≤ Apache-2.0 whitelist → community-tier.)
- The *exact/approx* column drives the determinism **capability flag** (see Further notes): only `exact`
  stores get the byte-determinism assertion; filter-pushdown / provenance / isolation conformance bind to
  **all** tiers regardless.

## Invariant binding (the conformance kit)

- **Provenance** — a record's `id` + `doc_id` + `source_locator` survive `upsert → query` intact.
- **Isolation** — the `where` pushdown is the **mechanism** by which sensitivity can be enforced *in the
  store*: a `sensitivity=RESTRICTED` record is excluded by `where={"sensitivity": "INTERNAL"}` even when its
  vector is identical to the query (the exact nearest neighbor). The authoritative RESTRICTED enforcement
  stays at the two existing exits (`retrieval/link`, `retrieval/rerank`); the store adds an **optional third
  pushdown point**, and the conformance test proves the filter is honored before scoring. (Honesty: without
  the filter the store does *not* auto-strip RESTRICTED — a test asserts this, so the filter test is
  meaningful rather than vacuous.)
- **Determinism** — every registered store yields identical, stably-ordered results across runs/instances.

## Testing decisions (TDD — written red first, in `tests/conformance/`)

The suite is **parametrized over `VECTOR_STORE_FACTORIES`** (conftest) so a future Qdrant/pgvector adapter
inherits every case by registering one line. Two files:

- **`test_vector_store_contract.py`** (behavior contract): upsert returns count · empty-store query → `[]` ·
  upsert→query returns the id · ranks by descending cosine · identical vector → score ≈ 1.0 first ·
  orthogonal → 0.0 · `k` caps · `k` > size returns all · default `k` = 50 · **tie-break id-ascending,
  stable** · `where` single-key · `where` AND multi-key · **filtered-out excluded even when nearest** ·
  `where` no-match → `[]` · `where` on absent key excludes · **upsert same id replaces (no dup)** ·
  delete-by-where returns count + removes · re-ingest idempotency · count tracks upsert/delete · **mixed
  dims → ValueError** (within a call and across calls) · **query dim mismatch → ValueError** · zero query
  vector → scores 0, no crash · zero stored vector → no crash · empty upsert → 0 · metadata round-trips.
- **`test_vector_store_invariants.py`** (invariant binding): provenance (id non-empty + in upserted set;
  doc_id/source_locator round-trip) · isolation (RESTRICTED excluded by filter even as nearest neighbor;
  and *present* without the filter) · determinism (two queries identical; two independent instances
  identical; tie-break stable across runs).

Red expectation: all of `tests/conformance/` errors at collection (conftest imports the not-yet-built
`ragspine.retrieval.vector.store`) until the module lands — the standard repo red→green TDD flow. The
existing suite stays green (a sub-package conftest error is isolated to that sub-package).

## Out of scope (v1)

- **Real ANN / HNSW / IVF indexing.** The default is brute-force cosine; ANN is an adapter concern.
- **A persistence backend for the default.** `InProcessVectorStore` stays in-memory by design; durable
  persistence is the `SqliteVecVectorStore` adapter's job, **now made real end-to-end** —
  `NarrativeIndex` embeds-and-persists at ingest, invalidates by `doc_id`, and retrieves store-managed
  (no chunk re-embedding on a fresh process). The sensitivity guardrail shipped as the swappable
  **`PersistencePolicy`** seam (default `IsolationFirstPolicy` never persists a RESTRICTED vector at
  rest; `PersistEverythingPolicy` is opt-in for a db classified RESTRICTED-tier per `docs/invariants.md`).
  Persisting into `ChunkStore` itself proved unnecessary — the seam already owns persistence.
- ~~**Rewiring `HybridRetriever`** to delegate to the store~~ — **done** (see Scope guard).
- ~~**The `[vector]` extra** and the **first real adapters**~~ — **done**: `[vector]` ships `sqlite-vec`
  (embedded) and `pg8000` (for pgvector), and both `SqliteVecVectorStore` + `PgVectorVectorStore` pass the
  whole conformance kit (pgvector against a `RAGSPINE_PG_URL` Postgres; it *skips* in the default no-server
  CI). **Further adapters** (Qdrant/Milvus/FAISS) remain later, each inheriting the conformance
  parametrization with one registration line.
- **Embedding generation** (owned by `EmbeddingBackend`), **RRF fusion / rerank** (owned by the retriever),
  and **entry-point auto-discovery** of third-party stores (the conftest list is the registry for now).

## Further notes

- This is the concrete first instance of the parent PRD's thesis: a commodity seam (🔧) gets a `Protocol` +
  offline default + conformance, so breadth (Qdrant/pgvector/…) can be *adapted* without the spine rotting.
- Complements [`prd-pipeline-topology-export.md`](prd-pipeline-topology-export.md): once the vector channel
  resolves to a named store, `.topology()` can render *which* store the assembled pipeline uses.
- **Exact vs approximate (the one non-obvious caveat for config-swap):** the in-process default does *exact*
  brute-force cosine, so byte-identical determinism holds. Real ANN backends (Milvus/Qdrant HNSW/IVF) are
  *approximate* — they may not reproduce byte-identical tie-breaks. So the conformance suite must carry a
  per-impl **capability flag** (`exact` vs `approximate`): the byte-determinism assertion runs only for
  `exact` stores; `approximate` stores instead assert weaker guarantees (stable ordering within one instance
  for identical calls; a recall@k floor against the exact default). pgvector with an exact scan is `exact`;
  with an HNSW index it is `approximate`. The filter-pushdown, provenance, and isolation conformance still
  apply to **all** backends regardless of this flag — those are the invariants that must never bend.
- **Scope discipline for "configurable":** keep the `Protocol` at the lowest-common-denominator the
  invariants need (`upsert` / `query+where` / `delete` / `count`). Backend-specific knobs (Milvus index
  params, pgvector `lists`/`probes`, payload indexing, quantization) live in that backend's own config
  section, **not** in the core `Protocol` — otherwise the abstraction leaks and you are back to maintaining
  each backend's full surface, which defeats the point.
