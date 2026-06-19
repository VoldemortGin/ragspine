---
covers:
  - src/ragspine/retrieval/vector/store.py
  - src/ragspine/retrieval/vector/adapters/sqlite_vec.py
  - src/ragspine/retrieval/vector/adapters/pgvector.py
  - src/ragspine/retrieval/vector/adapters/qdrant.py
  - src/ragspine/retrieval/vector/persistence_policy.py
  - src/ragspine/retrieval/lexical/retrieval.py
verified-against: 4fd1f4801816ecc3325a61aee129e374653bc75b
---

# VectorStore seam — the pluggable vector index, and how it wires into retrieval

Deep dive behind [`docs/prd-vector-store-seam.md`](../../../../docs/prd-vector-store-seam.md).
The PRD is the originating spec; this is the live contract. Two halves:

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
- **Scoring is a full-scan + Python cosine**, not vec0's native KNN `MATCH`. vec0's `k` is capped at
  4096 and it returns a `NULL` distance for zero vectors; a full scan sidesteps both and keeps the
  scoring byte-aligned with the default (so it registers as the **`exact` capability** in the
  conformance kit — full byte-determinism, not `approximate`; see "Capability flag" below).
  Native-KNN acceleration with exact float64 re-rank is a scale-time optimization, out of scope for
  the first adapter.
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
- **`where` is pushed to SQL, but scoring stays in Python.** The filter becomes a JSONB
  `meta->>'k' = v` AND-chain (the isolation-relevant pushdown, and it cuts rows transferred); the
  cosine, the exact-`0.0` zero-vector rule, and the id-ascending tie-break are computed in Python via
  the shared `store._cosine` — because pgvector's native `<=>` returns **NaN** for a zero vector and
  its distance-ordering doesn't match "id-asc among equal similarity." Native HNSW/IVFFlat KNN is the
  scale-time optimization, out of scope for the first adapter (same posture as sqlite-vec).

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
- **Scoring is a full-scroll + Python cosine** (same posture as sqlite-vec/pgvector): every point is
  scrolled back and the `where` filter, cosine, exact-`0.0` zero-vector rule, and id-ascending
  tie-break are computed in Python via the shared `store._cosine` / `store._matches`. The collection
  uses `Distance.DOT` (no normalization, raw store/fetch) precisely because Qdrant's own metric is
  irrelevant here — re-scoring in Python guarantees the contract semantics regardless of Qdrant
  internals (Qdrant's cosine normalizes vectors and is undefined on a zero vector). Native HNSW KNN is
  the scale-time optimization, out of the first adapter.
- **String `id` → deterministic UUID5 point id.** Qdrant point ids must be `uint`/`UUID`, but a
  `chunk_id` is an arbitrary string. The adapter maps `chunk_id → uuid5(namespace, chunk_id)` (same
  string ⇒ same point id ⇒ `upsert` replaces), and stores the **original** string id in the payload so
  `VectorHit.id` round-trips the original. Metadata lives under a reserved `__meta__` payload key, so
  provenance (`doc_id` / `source_locator`) survives intact.
- **It is registered `approximate`, by design** even though local mode (with Python re-scoring) is
  *incidentally exact*. The adapter's honest production guarantee is HNSW = approximate; declaring it
  so means the conformance contract won't over-constrain a future switch to native indexed KNN. See
  "Capability flag" below.

Select by config: `make_vector_store("qdrant")` / `make_vector_store("qdrant", path=…, collection=…)`
/ `RAGSPINE_VECTOR_STORE=qdrant`. `.topology()` names it (`向量通道 · QdrantVectorStore`).

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
