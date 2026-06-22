---
covers:
  - src/ragspine/retrieval/
verified-against: 3c6bf0b
---

# retrieval — agent contract

Auto-loaded when working under `src/ragspine/retrieval/`. Keep terse; deep dives go
in `src/ragspine/retrieval/docs/`.

## What lives here

Narrative RAG. `chunking/` (paragraph-granular chunker + versioned store; the
`Chunker` seam — `chunker.py`: a `@runtime_checkable` `Chunker` Protocol + a
`DefaultChunker` that **delegates byte-identically** to `chunk_document` (entry
point/signature preserved, all callers untouched) + `make_chunker` /
`RAGSPINE_CHUNKER` config selector with `ragspine.chunkers` entry-point discovery,
so semantic / contextual / parent-child strategies become swappable),
`lexical/` (Okapi BM25, CJK uni+bigram, RRF fusion — `HybridRetriever` delegates
its vector **scoring** to the `VectorStore` seam), `vector/` (injectable embedding
backends, default none = pure BM25; + the pluggable `VectorStore` seam — `store.py`
+ `make_vector_store` (a lazy-loader **registry** over the built-ins, with an
**entry-point auto-discovery** fallback on the `ragspine.vector_stores` group so a
third-party backend is selectable by name with no core PR) — with an invariant-binding conformance kit in
`tests/conformance/` carrying an **exact-vs-approximate capability flag**, three real adapters
behind `[vector]` — `adapters/sqlite_vec.py` (embedded, exact) + `adapters/pgvector.py`
(Postgres, pg8000/BSD, exact) + `adapters/qdrant.py` (HNSW, qdrant-client local mode, **approximate**),
all three now scaling via **native ANN/KNN** (vec0 `MATCH` / pgvector HNSW / Qdrant HNSW) that narrows a
candidate pool then an **exact `_cosine` re-rank** finalizes top-k (`store._pool_size` + `store._rerank`;
the pool covers the true top-k for the conformance datasets so `sqlite_vec`/`pgvector` stay exact)
— and `persistence_policy.py` gating what is written at rest), `rerank/` (LLM
listwise reranker, RRF-fallback), `link/` (adapter wiring retrieval into the agent).

## Invariants

- **RESTRICTED isolation** — sensitivity-`RESTRICTED` content is stripped at two
  exits, `link/` and `rerank/`, before it can reach a prompt. Both must stay. The
  `VectorStore.where` pushdown is an *optional third* enforcement point, never a
  replacement — the retriever's `where` carries the 5 recall dims, never `sensitivity`.
- **At-rest persistence** — the default `PersistencePolicy` (`IsolationFirstPolicy`)
  **never persists a `RESTRICTED` chunk's vector** at ingest; only `PersistEverything`
  (opt-in, RESTRICTED-tier db) does. See `docs/invariants.md` + `docs/vector-store.md`.

## Read before editing

- **Vector wiring is byte-identical on purpose.** `HybridRetriever` routes vector
  scoring through `VectorStore.query`, not an inline cosine loop. To keep results
  bit-stable: embed **candidates only** (prefilter strictly before any `embed_texts`),
  pass `k=len(candidates)`, build `where` with the exact `if val is not None` rule
  (so `""` is a real filter), and keep `best_vector` defaulting via `.get(cid, 0.0)`.
  A captured golden pins the triples — don't weaken it. Candidate `chunk_id`s are
  assumed unique (the `by_id` dict already does).
- **`NarrativeIndex` embeds-and-persists at ingest** (policy-gated), invalidates by
  `doc_id` (`delete(where={"doc_id": …})`, *not* blast-all), and retrieves with
  `HybridRetriever(manage_vectors=False)` — the retriever queries the store and never
  re-embeds chunks. Keep `_record_metadata`'s `doc_id` (it powers doc-scoped delete and
  is *not* in the retrieval `where`, so scoring stays byte-identical). The direct
  `HybridRetriever` path keeps `manage_vectors=True` (lazy embed) and stays byte-identical.

## Deep dives

- [`docs/vector-store.md`](docs/vector-store.md) — the `VectorStore` seam, its
  byte-identical wiring into `HybridRetriever`, the sqlite-vec / pgvector / qdrant adapters,
  the exact-vs-approximate capability flag, the isolation pushdown, and sensitivity-gated
  persistence (`PersistencePolicy` + embed-at-ingest).
- [`docs/chunker.md`](docs/chunker.md) — the `Chunker` seam: the `Protocol`, the
  `DefaultChunker` byte-identical delegation to `chunk_document`, the `make_chunker`
  factory + entry-point discovery, and the provenance conformance pack.
