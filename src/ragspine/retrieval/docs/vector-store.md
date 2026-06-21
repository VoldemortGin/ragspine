---
covers:
  - src/ragspine/retrieval/vector/store.py
  - src/ragspine/retrieval/vector/adapters/sqlite_vec.py
  - src/ragspine/retrieval/vector/adapters/pgvector.py
  - src/ragspine/retrieval/vector/adapters/qdrant.py
  - src/ragspine/retrieval/vector/persistence_policy.py
  - src/ragspine/retrieval/lexical/retrieval.py
verified-against: b81320e1b9604a0be3d5f24fdaf3ce651f4832a5
---

# VectorStore seam — the pluggable vector index, and how it wires into retrieval

本文件是 VectorStore 缝的权威 live 契约（原始 PRD 已退役，历史见 git）。Two halves:

1. **The seam** (`vector/store.py`) — a `Protocol` + a zero-dependency offline default, with the
   three invariants (provenance / isolation / determinism) bound by `tests/conformance/`.
2. **The wiring** (`lexical/retrieval.py`) — `HybridRetriever` delegates its vector **scoring** to
   that seam instead of an inline cosine loop, so a real Qdrant / pgvector / sqlite-vec backend can
   be dropped in by config without touching the retriever.

## The seam

`VectorStore` owns exactly one concern: **store vectors + answer one filtered top-k similarity
query.** It does *not* own BM25, RRF fusion, or rerank — those stay in `HybridRetriever`.

```python
class VectorStore(Protocol):
    def upsert(self, records: Sequence[VectorRecord]) -> int: ...
    def query(self, vector, *, k=DEFAULT_QUERY_K, where=None) -> list[VectorHit]: ...
    def delete(self, *, where: Mapping[str, str]) -> int: ...
    def count(self) -> int: ...
```

`InProcessVectorStore` is the default: brute-force cosine + `id`-ascending tie-break, byte-for-byte
equivalent to the cosine loop the retriever used before wiring. `cosine` matches
`retrieval.cosine_similarity` exactly (zero vector → `0.0`). One shared dimension; a mixed-dimension
upsert or a wrong-dimension query raises `ValueError` — never a silently bad vector.

`make_vector_store(spec)` is the config-string factory (mirrors `make_embedding_backend`):
`none` → `None` (retriever self-builds the in-process default), `in_process` →
`InProcessVectorStore`, `sqlite_vec` / `pgvector` / `qdrant` → the adapter behind `[vector]`. Env
override: `RAGSPINE_VECTOR_STORE`. Selectable end-to-end via `ServiceConfig.vector_store`.

Built-in names resolve through a **lazy-loader registry** (`_BUILTIN_LOADERS`, plus aliases;
case/whitespace-insensitive) — not an `if`-ladder — each loader importing only its (SDK-free) adapter
*module*, so the SDK stays lazy until the returned class is instantiated (core imports zero SDKs even
when a built-in adapter is *selected*). An **unknown** name then falls back to **entry-point
auto-discovery**: `make_vector_store` looks it up in the `ragspine.vector_stores` entry-point group
(`VECTOR_STORE_ENTRY_POINT_GROUP`) via `importlib.metadata.entry_points`, so a third-party package
(`ragspine-foo`) registers a backend by name with **no core PR** — the last leg of the five-part
extension contract (Protocol + default + adapter + registry/discovery + conformance). A built-in name
**wins over** a same-named entry point (third parties can't hijack built-in semantics); a name that is
neither raises a `ValueError` listing the built-in + discovered names; a selected-but-uninstalled
backend keeps raising the actionable `pip install ragspine[vector]` message from the adapter's
`__init__`. (The `tests/conformance/` conftest stays the **explicit** registry for the conformance
parametrization — discovery selects a backend to *run*, the conftest list is what binds the
invariants.)

## The wiring (byte-identical by construction)

`HybridRetriever.__init__` resolves `self.vector_store`: when an `embedding_backend` is present it
uses the injected store or builds an `InProcessVectorStore`; pure-BM25 leaves it `None`. In
`search`, the inline `cosine_similarity` loop is replaced by:

1. embed **missing candidate texts only** into `_embedding_cache` (the prefilter-before-scoring
   invariant is preserved — a filtered-out chunk's text never reaches `embed_texts`);
2. `upsert` the candidate vectors as `VectorRecord`s (metadata = the 5 recall dims, str-coerced);
3. per query variant, `vector_store.query(query_vec, k=len(candidates), where=...)` for the vector
   ranking and per-candidate score.

Equivalence rests on four facts, all conformance-bound: the store sorts by `(-score, id)`
identically to the old loop; `k=len(candidates)` returns **every** candidate including zero-cosine
ones (so `best_vector` is populated for all, exactly as the inline `zip(sims, candidates)` did);
`where` replicates the Python prefilter exactly (a dim is a filter **iff its value is not None**, so
`""` is a real filter value, never omitted); and a `h.id in by_id` guard keeps results scoped to the
current candidates even against a shared/superset store. A regression golden
(`tests/retrieval/lexical/test_retrieval_vector_store.py::test_byte_identity_golden`) pins the exact
`(bm25, vector, fused)` triples captured from the pre-wiring implementation.

**Precondition:** candidate `chunk_id`s are unique (the existing `by_id = {c.chunk_id: c}` already
assumes this). Duplicate ids are out of the supported input space.

### Lifecycle / invalidation

`NarrativeIndex` owns one `vector_store` and populates it **at ingest** (see "Persistence" below) —
there is no separate `_embedding_cache`; the store *is* the durable vector source. Each `ingest`
re-ingests a doc with an **unconditional, doc-scoped** `delete(where={"doc_id": d})` *before* the
(policy-gated) re-upsert of the new version's vectors. That unconditional delete is the load-bearing
correctness property: it sweeps every old `chunk_id` of doc *d* (regardless of seq drift, shrinking
chunk count, or a same-`chunk_id`-new-text rewrite), so no stale or now-RESTRICTED vector can survive
a re-ingest. Other docs' vectors are untouched.

## Isolation: `where` is a third pushdown point, not a RESTRICTED exit

The `where` filter the retriever passes carries only the **5 recall dims**, never `sensitivity`.
RESTRICTED content still rides through the retriever and is stripped at the two authoritative exits
(`retrieval/link`, `retrieval/rerank`) — wiring the store changed nothing here. The store's filter
pushdown is an *optional* third enforcement point (proven by the conformance isolation test: a
RESTRICTED record is excluded by `where={"sensitivity": "INTERNAL"}` even when it is the exact
nearest neighbor, and — honest negative control — is *present* without the filter). The retriever
does not use it for RESTRICTED; the capability simply exists for a backend that wants store-level
enforcement.

## Topology

`HybridRetriever.topology()` names the resolved store: the `vector` node's label becomes
`向量通道 · <StoreClass>` and its `symbol` is the store class's dotted path (drift-guard-resolvable).
So the diagram tells the truth about *which* vector backend this pipeline assembled.

## Adapter #1: sqlite-vec (`vector/adapters/sqlite_vec.py`)

The first real backend, behind the `[vector]` extra (lazy-imported; the core still runs on the
in-process default with zero deps). It stores vectors in a **sqlite-vec `vec0` virtual table**
(`id TEXT PK, embedding float[N] distance_metric=cosine, +meta TEXT`), default `:memory:`, optional
`db_path` for **durable persistence across process restarts** — the thing the in-process default
can't do.

It passes the **entire** conformance kit (66 cases) by registering one line in the conftest, which
is the whole point of the seam: an adapter that broke provenance / isolation / determinism would go
red here, not in production. It clears the kit because it reuses `store._cosine` and `store._matches`
directly — so scoring, the exact-`0.0` zero-vector rule, the id-ascending tie-break, and the
"absent key excludes" `where` semantics are **identical** to the in-process default, not
re-implemented.

Two deliberate, documented choices:
- **Scoring is native vec0 KNN `MATCH` (candidate pool) + an exact Python re-rank** — see
  [Native ANN/KNN + exact re-rank](#native-annknn--exact-re-rank-shipped) below. The pool is
  `max(k, ef_search, min(count, pool_ceiling))` (capped at vec0's `k`-limit 4096); for a store at/under
  `pool_ceiling` (every conformance store) the pool covers **all** rows — vec0 returns even the
  `NULL`-distance zero vectors when `k ≥ count` — so the exact re-rank reproduces brute-force
  byte-for-byte and it stays the **`exact` capability** (full byte-determinism, not `approximate`). The
  `where` filter is applied in the re-rank (vec0 can't filter the JSON `+meta` aux column inside KNN);
  because the pool covers all rows for conformance, the filter output is correct and isolation never
  leaks (a RESTRICTED nearest neighbor is dropped by the re-rank, never surfaced). `ef_search` /
  `pool_ceiling` are this adapter's own kwargs, **not** the core Protocol.
- **`upsert` is DELETE-then-INSERT** (vec0 rejects `INSERT OR REPLACE`), preserving id-replace
  semantics. Re-open recovers the dimension from a stored vector, or from the `float[N]` schema when
  the table is empty.

Selectable by config: `make_vector_store("sqlite_vec")` / `RAGSPINE_VECTOR_STORE=sqlite_vec`, and it
flows through `build_narrative_retriever` / `ServiceConfig` exactly like the default. `.topology()`
names it (`向量通道 · SqliteVecVectorStore`).

## Adapter #2: pgvector (`vector/adapters/pgvector.py`)

The first **networked / shared** backend — PostgreSQL + the pgvector extension, for when the vector
index must be shared across processes/hosts. Two deliberate choices set it apart:

- **Driver is `pg8000` (pure-Python, BSD), *not* psycopg.** psycopg is LGPL, which ADR 0009's
  ≤ Apache-2.0 license gate excludes; pg8000 is permissive. The adapter speaks plain SQL, so it needs
  no pgvector-specific Python package.
- **Native HNSW KNN (candidate pool) + an exact Python re-rank, with `where` always pushed to SQL.**
  The table carries an **HNSW (`vector_cosine_ops`) index** on `embedding`; the filter becomes a JSONB
  `meta->>'k' = v` AND-chain (the isolation-relevant pushdown, and it cuts rows transferred). Pool size
  is `max(k, ef_search, min(count, pool_ceiling))`. For a store at/under `pool_ceiling` (every
  conformance store) the query is a **plain `SELECT … WHERE <where>` full scan** (no `ORDER BY <=>`),
  returning **all** matching rows including zero vectors — this sidesteps pgvector's `<=>` returning
  **NaN** for a zero vector (which the HNSW index drops from a `NULL`/NaN-ordered result); the exact
  Python re-rank then reproduces brute force byte-for-byte (the **`exact` capability** holds). Above
  `pool_ceiling` it switches to `SET hnsw.ef_search` + `… WHERE <where> ORDER BY embedding <=> q LIMIT
  pool` — the native index narrows, then the same exact re-rank finalizes (Python sidesteps the `<=>`
  NaN + the id-asc tie-break). Index params (`m` / `ef_construction` / `ef_search` / `pool_ceiling`)
  are this adapter's own kwargs, **not** the core Protocol. See
  [Native ANN/KNN + exact re-rank](#native-annknn--exact-re-rank-shipped).

Table lifecycle splits the two needs cleanly: **`table=None` → a session `TEMP` table** (auto-dropped
on disconnect — every conformance instance is isolated with zero leftover), **a named `table=` → a
persistent `CREATE TABLE IF NOT EXISTS`** that survives across connections (the real value; dimension
is recovered from the `vector(N)` column's `atttypmod` on reopen). `upsert` is native `INSERT … ON
CONFLICT (id) DO UPDATE`, wrapped in a transaction with rollback.

Connection via `RAGSPINE_PG_URL` (`postgresql://user[:pass]@host:port/db`) or an explicit `dsn=`.
**Conformance binding is conditional:** it runs (and gates) only when `RAGSPINE_PG_URL` points at a
Postgres with pgvector; in the default no-server CI the `pgvector` params **skip** (yellow, not red) —
a server backend can't be required of every contributor. It was verified green against a local
Postgres 17 + pgvector 0.8.0. Select by config: `make_vector_store("pgvector", dsn=…)` /
`RAGSPINE_VECTOR_STORE=pgvector`.

## Adapter #3: Qdrant (`vector/adapters/qdrant.py`)

The first **`approximate`-capability** backend — Qdrant (HNSW), behind `[vector]` via the
**`qdrant-client` (Apache-2.0)** driver (permissive, passes ADR 0009's ≤ Apache-2.0 gate, same tier
as sqlite-vec). It runs in Qdrant **local mode** so conformance is purely in-process with **no
server**: `QdrantClient(location=":memory:")` for ephemeral isolated instances, `QdrantClient(path=)`
for a durable named collection that survives across processes. Because local mode needs no server, the
`qdrant` conformance params gate **unconditionally** (whenever `qdrant-client` is installed) — unlike
pgvector's `RAGSPINE_PG_URL` gate.

Three deliberate choices:
- **Native HNSW `query_points` (candidate pool) + an exact Python re-rank, with `where` pushed as a
  payload filter.** The search narrows to `limit = max(k, ef_search, min(count, pool_ceiling))` points
  (the `where` becomes a nested `__meta__.<key>` `FieldCondition` AND-filter — the isolation pushdown,
  and it cuts points transferred); the cosine, exact-`0.0` zero-vector rule, and id-ascending tie-break
  are computed in Python via the shared `store._cosine` / `store._matches`. The collection still uses
  `Distance.DOT` (no normalization, raw store/fetch) — so the **native search narrows by dot product
  while the re-rank scores by cosine**, which is exactly why Qdrant is **`approximate`**: dot-nearest
  ≠ cosine-nearest in general. For a store at/under `pool_ceiling` (every conformance store) the pool
  covers all rows, so it is *incidentally* exact, but the contract only holds it to the weaker
  approximate guarantees. `delete` still full-scrolls (not a hot path). `ef_search` / `pool_ceiling`
  are this adapter's own kwargs, **not** the core Protocol. See
  [Native ANN/KNN + exact re-rank](#native-annknn--exact-re-rank-shipped).
- **String `id` → deterministic UUID5 point id.** Qdrant point ids must be `uint`/`UUID`, but a
  `chunk_id` is an arbitrary string. The adapter maps `chunk_id → uuid5(namespace, chunk_id)` (same
  string ⇒ same point id ⇒ `upsert` replaces), and stores the **original** string id in the payload so
  `VectorHit.id` round-trips the original. Metadata lives under a reserved `__meta__` payload key, so
  provenance (`doc_id` / `source_locator`) survives intact.
- **It is registered `approximate`, by design** even though local mode (with the exact Python re-rank)
  is *incidentally exact*. The adapter's honest production guarantee is HNSW = approximate; declaring it
  so is exactly what lets it now narrow with **native HNSW search** (dot-product candidate pool) without
  the conformance contract over-constraining it. See "Capability flag" below.

Select by config: `make_vector_store("qdrant")` / `make_vector_store("qdrant", path=…, collection=…)`
/ `RAGSPINE_VECTOR_STORE=qdrant`. `.topology()` names it (`向量通道 · QdrantVectorStore`).

## Native ANN/KNN + exact re-rank (shipped)

All three adapters now **scale via their native index**, not a full scan/scroll — without losing the
contract. The shape is identical across them (two shared helpers in `store.py`, reused like
`_cosine` / `_matches` so the scoring口径 stays byte-aligned with the in-process default):

1. **Candidate pool via the native index.** `_pool_size(k, ef_search, count, ceiling) =
   max(k, ef_search, min(count, ceiling))`. sqlite-vec uses vec0 `MATCH … AND k = pool`; pgvector uses
   `ORDER BY embedding <=> q LIMIT pool` over the **HNSW (`vector_cosine_ops`) index**; Qdrant uses
   `query_points(limit=pool)` over its HNSW. The `where` filter is **pushed into the native query**
   where the backend allows it (pgvector SQL `WHERE`, Qdrant payload `FieldCondition`) so a RESTRICTED
   row is excluded *within* the ANN; sqlite-vec can't filter its JSON `+meta` aux column inside KNN, so
   it filters in the re-rank instead (still correct + isolation-safe).
2. **Exact re-rank.** `_rerank(candidates, query, k, where)` re-scores the pool with the exact
   `_cosine`, applies `where` via `_matches` (the authoritative filter — zero-vector → `0.0`,
   "absent key excludes"), sorts by `(-score, id)`, and returns top-k. This is the **exact re-rank**
   that finalizes top-k.

**Why this preserves exactness for the `exact` tier.** The pool is *always* at least
`min(count, pool_ceiling)`, so for a store at/under `pool_ceiling` — **every conformance store** — the
pool covers **all** matching rows, and the exact re-rank reproduces brute-force cosine **byte-for-byte**
(`sqlite_vec` and `pgvector` stay `exact`). Above the ceiling the native index narrows first, then the
exact re-rank still finalizes top-k from that pool — the scale win. pgvector additionally **falls back
to a plain full-scan when `pool ≥ count`** (no `ORDER BY <=>`), because its `<=>` returns NaN for a zero
vector and the HNSW result drops NaN-ordered rows; sqlite-vec's vec0 and Qdrant's `Distance.DOT` both
return zero-vector rows from the native query, so they query natively throughout. `ef_search` /
`pool_ceiling` / pgvector's `m` / `ef_construction` are each **adapter-private kwargs**, never the core
`Protocol` (keeping the seam at the invariant lowest-common-denominator). Qdrant stays **`approximate`**
because its native search narrows by dot product while the re-rank scores by cosine.

## Capability flag: exact vs approximate (the conformance kit's determinism tier)

The conformance registry (`tests/conformance/conftest.py::VECTOR_STORE_IMPLS`) is a
**name → capability** map (`exact` / `approximate`), exposed to tests via the `vector_store_capability`
fixture. The **three determinism tests** in `test_vector_store_invariants.py` branch on it:

- **`exact`** (`in_process`, `sqlite_vec`, `pgvector`) — the **full byte-identical** assertions,
  unchanged in strength: repeated queries are `(id, score)`-identical, two independent instances agree
  byte-for-byte, and ties resolve `id`-ascending stably.
- **`approximate`** (`qdrant`) — the weaker PRD guarantees an HNSW backend can actually honor: stable
  *ordering* within one instance across identical repeated calls, plus a **recall@k floor** against the
  exact `InProcessVectorStore` default (for clearly-separated vectors the approximate top-k recovers the
  same id set). The id-ascending tie-break and byte-identical scores are **not** required of it.

Everything else binds **fully to every tier regardless of the flag** — `where` filter-pushdown,
provenance round-trip, and RESTRICTED isolation are invariants that never bend, approximate or not.
Adding the next approximate backend (Milvus, a future HNSW-indexed pgvector) is **one registry line**
(`"milvus": "approximate"`) plus a `_resolve_impl` branch — the determinism tier follows automatically.

## Persistence, made real — and sensitivity-gated (`persistence_policy.py`)

Persistence is not a new `ChunkStore` column; it's the **`VectorStore` seam doing its job** (point a
`SqliteVecVectorStore` at a `db_path`). For that persistence to actually pay off, `NarrativeIndex`
changed in three ways:

- **Embed-and-persist at ingest.** When an embedding backend is present, `ingest` embeds the doc's
  chunks and `upsert`s their vectors into the store *then* — not lazily at query. So the durable
  store holds the vectors before any query runs.
- **`doc_id`-scoped invalidation.** Re-ingesting doc *d* does `delete(where={"doc_id": d})` (not the
  old `delete(where={})` blast-all), so other docs' persisted vectors survive across ingests. This is
  why `_record_metadata` now carries `doc_id` (it is *not* part of the retrieval `where`, so scoring
  stays byte-identical).
- **Store-managed retrieve.** `NarrativeIndex` builds its `HybridRetriever` with
  `manage_vectors=False`: the retriever embeds only the *query* and calls `store.query`, never
  re-embedding chunks. So a fresh process over the same `db_path` retrieves with **zero chunk
  re-embedding** — the persistence pays off. (The direct `HybridRetriever` path keeps
  `manage_vectors=True` and stays byte-identical.)

The **`PersistencePolicy` seam** (one method, `persistable(chunk) -> bool`) gates *what* is written at
rest — a minimal, swappable Protocol, deliberately one-decision to avoid a framework-style
god-interface:

- **`IsolationFirstPolicy` (default)** — never persists a `RESTRICTED` chunk's vector. A persisted
  embedding is a recoverable derivative of the chunk text stored next to its lineage; persisting a
  RESTRICTED one would be a new at-rest surface bypassing the two isolation exits. So by default it
  simply isn't written — a RESTRICTED chunk still retrieves via BM25 (vector score 0) and is stripped
  at the `link`/`rerank` exits as before. A NarrativeIndex test binds this: with the default policy, a
  RESTRICTED doc contributes **zero** records to the store.
- **`PersistEverythingPolicy` (opt-in)** — persists all, *only* when the whole vector db is itself
  classified RESTRICTED-tier at rest (see `docs/invariants.md`).

Selectable by config: `make_persistence_policy("default" | "persist_everything")` /
`RAGSPINE_PERSISTENCE_POLICY`, threaded through `build_narrative_retriever` / `ServiceConfig`.
