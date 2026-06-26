# PRD — Breadth via Adapters: the extension contract & capability matrix

> **status:** in progress (P0 `VectorStore` seam wired; P1 `SourceConnector` seam shipped; P0 `Extractor` registry + `Chunker` Protocol formalized; P0 pipeline-topology shipped) · **created:** 2026-06-17 · **methodology:** TDD (conformance tests first)
> Living backlog — the seams/adapters tracked here land incrementally; it carries no `covers:` frontmatter
> (each seam's shipped contract doc lives under `src/ragspine/<domain>/docs/*.md`).
> Realizes [ADR 0003](adr/0003-audience-oss-library.md) (general-purpose OSS library), operating within
> [ADR 0005](adr/0005-lean-core-experimental-isolation.md) (lean core + extras) and
> [ADR 0009](adr/0009-dependency-and-framework-policy.md) (no orchestration lock-in, permissive-license-only).
> Once a seam ships, its contract doc lives at `src/ragspine/<domain>/docs/*.md` with `covers:`.
> **Companion:** [`prd-quality-depth.md`](prd-quality-depth.md) spends the ⭐ quality budget — semantic default,
> local cross-encoder rerank, spine-family extraction/OCR integration, contextual/parent-child chunking,
> groundedness eval, and GraphRAG (W7). This PRD *rents the 🔧 commodity surface*; that one *out-engineers the
> ⭐ stages and proves the 🛡 invariants*.

## Problem statement

ADR 0003 commits RAGSpine to being a **general-purpose** RAG library that others build on. "General-purpose"
is only credible with **breadth**: the connectors, formats, vector stores, embedding/LLM/rerank backends a
real deployment expects. The mature frameworks (LangChain / LlamaIndex / Haystack / RAGFlow) win on exactly
this — hundreds of integrations maintained by a company plus a community.

A single-author project **cannot and must not** match that by self-authoring every integration. Breadth is
not a *build-once* problem; it is a *maintain-forever* treadmill — connector APIs drift, vector stores appear
monthly, embedding models churn quarterly. Most of that surface is **commodity plumbing**: low value (everyone
has it, so it wins no users) and unbounded maintenance cost.

But breadth cannot simply be thrown open either: RAGSpine's identity is a set of **code-enforced invariants**
(anti-fabrication, provenance, RESTRICTED isolation, privacy-aware traces — see `docs/invariants.md`). A naive
third-party retriever or loader could silently break any of them. The risk of "open it to adapters" is that
the spine rots.

**The need:** a way to get breadth *perception* at a fraction of the maintenance cost, while making it
**structurally impossible** for an adapter to violate an invariant.

## Strategy (the decision)

> Don't compete on *vanity breadth* (integration count). Compete on *quality breadth* + *guarantees*.
> **Own the spine; own the quality-critical stages; absorb the commodity surface via adapters.**

Every pipeline stage is one of three kinds; the kind dictates who writes and maintains it:

| Mark | Kind | Policy |
|---|---|---|
| 🛡 | **touches an invariant** (provenance, anti-fabrication, isolation, privacy) | **own it**, in core, conformance-locked — this is the moat |
| ⭐ | **quality-critical** (decides whether the answer is *correct*) | **own it**, the place to out-engineer the breadth frameworks |
| 🔧 | **commodity plumbing** (everyone has it; value is low, churn is high) | **don't author it** — define a `Protocol`, ship an offline default, provide a *thin* adapter behind extras, let users/community extend |

This is not a new direction — it is the concrete realization of decisions already made (0003/0005/0009). What
this PRD adds is the **mechanism** (a uniform extension contract) and the **map** (a capability matrix with
status), so "breadth" becomes a tracked, bounded backlog instead of an open-ended ambition.

## The extension contract (the mechanism)

Every extensible seam follows the **same five-part contract**. This uniformity is the product: a contributor
who has added one backend can add any backend, and a user reads one mental model everywhere.

1. **`Protocol`** — a minimal, SDK-free structural interface at the seam (the only thing core imports).
2. **Offline default** — a real, deterministic implementation that runs with zero heavy deps, so
   `pip install ragspine` is runnable end-to-end (BM25 + Mock* today). Keeps the lean-default of ADR 0009.
3. **Thin adapter(s)** — a small wrapper over a best-in-class library, lazy-imported, gated behind an
   `extra` (`[embed]`, `[vector]`, …). The adapter *delegates*; it does not re-implement the library.
4. **Registry / discovery** — a name→factory lookup so a backend is selectable by string from config
   (`vector_store = "qdrant"`) without the core importing it. Third-party packages register via entry points.
5. **Conformance suite** — a shared, importable test pack the seam's `Protocol` defines; **any** implementation
   (first- or third-party) must pass it. This is where the invariants are bound (see below).

```python
# the shape every seam repeats
class Reranker(Protocol):                       # 1. Protocol
    def rerank(self, q: Query, hits: list[Hit]) -> list[Hit]: ...

class IdentityReranker:                          # 2. offline default (deterministic, dep-free)
    def rerank(self, q, hits): return hits

# src/ragspine/retrieval/rerank/adapters/cross_encoder.py   # 3. thin adapter, behind [rerank] extra
class CrossEncoderReranker:
    def __init__(self, model: str):
        from sentence_transformers import CrossEncoder   # lazy import
        ...

register("reranker", "identity", IdentityReranker)        # 4. registry
register("reranker", "cross-encoder", CrossEncoderReranker)
# tests/conformance/test_reranker.py parametrizes over every registered reranker  # 5. conformance
```

## Invariant-binding conformance kit (the differentiator)

The conformance suite is **the reason RAGSpine can open the floodgates and the breadth frameworks can't.**
Each `Protocol` ships with a parametrized test pack that asserts the relevant invariant for *every*
implementation registered against it — so an adapter that breaks the spine **fails CI**, not production:

- **Provenance** (`SourceConnector`, `Extractor`, `Chunker`): every emitted unit carries a non-null
  `source_doc_id` + locator; lineage survives the transform. An adapter that drops it fails.
- **RESTRICTED isolation** (any `Retriever` / `Reranker`): fed a `RESTRICTED` item, the output must not
  contain it. Re-asserts the two-exit rule at the *seam*, so a new retriever can't bypass it.
- **Privacy-aware traces** (`TraceSink`): a payload containing an answer / fact value / chunk text is
  rejected (or scrubbed). No sink — including OTel adapters — can leak content.
- **Anti-fabrication** (`LLMProvider`): an adversarial provider that *always fabricates* still yields
  "not found" when the structured channel returns no `found` fact (the guard is orchestrator-side, so this
  is a regression lock, proving provider-independence).
- **Determinism** (offline defaults): identical input → byte-identical output, so the default loop stays
  reproducible regardless of which adapters exist.

A contributor adding `QdrantVectorStore` doesn't *opt into* these tests — they inherit them by registering.
Breadth grows; the spine cannot rot.

## Seams to introduce or formalize

Current state (8 Protocols exist): `LLMProvider`, `IntentParser`, `NarrativeRetriever`, `OcrBackend`,
`EmbeddingBackend`, `QueryRewriter`, `ListwiseJudge` (reranker), `TaskQueue`. Gaps that block breadth:

- **`SourceConnector` (SEAM SHIPPED, 🔧)** — `iter_documents() -> Iterable[RawDoc]`. **Shipped seam-first**
  (standalone, like `VectorStore` was): `Protocol` + frozen `RawDoc` (`source_doc_id` + `locator` + raw
  `content` + `content_type`/`metadata`) + `FilesystemConnector` offline default (pathlib recursive walk,
  deterministic relative-POSIX order, `source_doc_id = path.name` — byte-identical to how ingestion identifies
  a file today) + `make_source_connector` / `RAGSPINE_SOURCE_CONNECTOR` config selector with entry-point
  auto-discovery (`ragspine.source_connectors` group), and a **provenance conformance pack** bound at the
  point of entry (every `RawDoc` carries non-null `source_doc_id` + locator, with a lineage-dropping stub
  proving it non-vacuous). Not yet wired into the existing narrative ingest path (behavior-preserving). This
  unlocks S3/Drive/Notion/HTTP later behind one Protocol. Remaining: the remote adapters (P1).
- **`VectorStore` (DONE, 🔧)** — `upsert(...)`, `query(vector, k, where) -> list[Hit]`, `delete`, `count`.
  **Shipped, wired, and adapted:** `Protocol` + `InProcessVectorStore` offline default (brute-force cosine) +
  conformance kit, `HybridRetriever` delegates vector scoring to it byte-identically, config-selected
  by `make_vector_store` / `RAGSPINE_VECTOR_STORE`, and **three real adapters — `sqlite-vec` (embedded),
  `pgvector` (PostgreSQL, pg8000/BSD), and `qdrant` (HNSW, local mode, Apache-2.0)** — behind `[vector]` each
  inherit the whole conformance kit, with Qdrant the first **approximate**-capability backend (the kit now
  carries an exact-vs-approximate flag). Metadata `where` pushdown carries isolation (third, optional
  enforcement point). Remaining: more adapters (Milvus/FAISS, P1). Was the single highest-leverage missing
  seam; it no longer is.
- **`Extractor` registry (FORMALIZED, ⭐/🔧)** — the extractors existed (PDF-digital, PPTX, XLSX, +styled)
  but were dispatched ad-hoc with no shared seam. **Shipped (behavior-preserving):** a `@runtime_checkable`
  `Extractor` `Protocol` (`extract(path) → list[StyledGrid]`) + a `mime/type → Extractor` registry
  (`extraction/registry.py`) — built-ins registered by mime via lazy loaders (zero SDK at import; thin
  `_FunctionExtractor` wraps the existing `extract_grids`), `register_extractor(mime, …)` to add a format with
  **no router edit**, `get_extractor(mime)` dispatch, and a typed `UnsupportedFormatError` (a `LookupError`,
  not a bare `KeyError`) for an unregistered mime. The per-page digital/scanned PDF triage stays in
  `routing/pdf_router.py`, untouched. Remaining: the new formats themselves (DOCX/HTML/MD/CSV via
  `unstructured`/`docling`, P1).
- **`Chunker` (Protocol shipped, ⭐)** — chunking existed as a concrete `chunk_document` module. **Shipped
  (behavior-preserving):** a `@runtime_checkable` `Chunker` `Protocol` + a `DefaultChunker` dependency-free
  default that **delegates byte-identically** to `chunk_document` (the entry point + signature are preserved,
  every caller untouched) + a `make_chunker(spec)` / `RAGSPINE_CHUNKER` config selector with entry-point
  discovery (`ragspine.chunkers` group), so semantic / contextual / parent-child strategies become swappable,
  quality-critical units. Remaining: those strategies (P1).
- **`TraceSink` (NEW Protocol, 🛡)** — formalize the privacy-aware trace sink so observability can fan out to
  OTel/files *through the privacy conformance test*, never around it.

## Capability matrix

Legend: **kind** 🛡/⭐/🔧 (own/own/adapt) · **status** ✅ have · ◐ partial · ✗ gap.

| Pipeline seam | Protocol | Kind | Offline default | Adapter targets (extra) | Status | Phase |
|---|---|---|---|---|---|---|
| Source connector | `SourceConnector` | 🔧 | **local filesystem ✅** | S3·GCS·Drive·Notion·Confluence·HTTP | ◐ seam | P1 seam ✓ · adapters P1 |
| Document extract | `Extractor` *(formalized)* | ⭐🔧 | PDF-digital·PPTX·XLSX | DOCX·HTML·MD·CSV via `unstructured`/`docling` `[pdf]` | ✅ registry | P0 reg ✓ · P1 fmts |
| OCR | `OcrBackend` | 🔧 | mock | paddleocr `[ocr]` | ✅ | — |
| Chunking | `Chunker` | ⭐ | recursive/structural | semantic · contextual · parent-child | ✅ proto | P0 proto ✓ · P1 strat |
| Embedding | `EmbeddingBackend` | 🔧⭐ | lexical-hash (non-semantic) | sentence-transformers `[embed]` · OpenAI `[llm]` | ✅ | — |
| Vector store | `VectorStore` | 🔧 | in-proc brute force | **sqlite-vec ✅ · pgvector ✅ · Qdrant ✅** · Milvus·FAISS·Chroma·LanceDB | ✅ seam + 3 adapters | P0 ✓ · more adapters P1 |
| Lexical index | *(built-in)* | ⭐ | BM25 | — | ✅ | — |
| Retrieve / fuse | `HybridRetriever` | ⭐ | BM25 + vector → RRF | — | ✅ | — |
| Rerank | `ListwiseJudge` | ⭐ | identity | cross-encoder · Cohere · BGE `[rerank]` | ✅ proto / ✗ adapters | P1 |
| Query transform | `QueryRewriter` | ⭐ | identity | multi-query · HyDE · self-query | ✅ proto | P1 |
| LLM provider | `LLMProvider` | 🔧 | MockProvider | Anthropic · OpenAI `[llm]` | ✅ | — |
| Intent parse | `IntentParser` | 🛡 | rule-based | LLM-based | ✅ | — |
| Task queue | `TaskQueue` | 🔧 | FakeQueue | RQ/Redis `[service]` | ✅ | — |
| Structured store | `FactStore` *(proto later)* | 🛡 | sqlite | DuckDB · Postgres | ✅ concrete / ✗ proto | P2 |
| Trace sink | `TraceSink` *(new)* | 🛡 | in-proc privacy-safe | OTel (privacy-filtered) | ◐ | P2 |
| Eval | *(golden sets)* | 🛡 | offline golden | RAGAS-compatible metrics | ✅ | P2 |

**Read of the matrix:** the spine (🛡) and the quality stages (⭐) are largely owned and present already. The
**P0 `VectorStore` seam is wired live with its first real adapters** — `Protocol` + offline default +
conformance kit + `HybridRetriever` delegation + `make_vector_store` config selector + `.topology()` naming +
**three real adapters, `sqlite-vec` (embedded), `pgvector` (PostgreSQL, pg8000/BSD), and `qdrant` (HNSW,
local mode)**, all conformance-bound behind `[vector]` (qdrant the first approximate-capability backend) — see
the live contract / deep dive
[`vector-store.md`](../src/ragspine/retrieval/docs/vector-store.md); what remains there is **more adapters**
(Milvus/FAISS, P1). The `SourceConnector` **seam is now shipped** (Protocol + `FilesystemConnector` default +
`make_source_connector` config selector + entry-point discovery + provenance conformance pack — see the deep
dive [`source-connector.md`](../src/ragspine/ingestion/docs/source-connector.md)); the remaining breadth gap
there is its **remote adapters** (S3/Drive/Notion/HTTP, P1). The **`Extractor` registry and `Chunker` Protocol
are now formalized** (behavior-preserving lifts of existing code — a `mime → Extractor` registry +
`get_extractor` dispatch, and a `Chunker` Protocol whose `DefaultChunker` delegates byte-identically to
`chunk_document`; see the deep dives [`extractor-registry.md`](../src/ragspine/extraction/docs/extractor-registry.md)
and [`chunker.md`](../src/ragspine/retrieval/docs/chunker.md)); the open commodity surface there is filling out
`Extractor` **formats** (DOCX/HTML/MD/CSV, P1) and `Chunker` **strategies** (semantic/contextual/parent-child,
P1) — exactly the surface that should be *adapted*, not authored.

### 已完成并退役的 PRD（live 契约见下）

These three originating PRDs shipped in full and were retired; their history lives in git. The work they
tracked now lives as live contracts / docs, and any remaining slices fold into the matrix above:

- **VectorStore seam** — complete; live contract `src/ragspine/retrieval/docs/vector-store.md` (Milvus/FAISS
  remain as **more adapters** in the matrix above).
- **Deployment / Helm** — shipped (Compose + Helm); live docs `deploy/README.md` + `deploy/helm/README.md`.
- **Pipeline topology** — shipped; live mechanism `HybridRetriever.topology()` + the git-ignored
  `docs/generated/` renders.

## Phasing

- **P0 — minimum credible breadth.** A user can run a *real* semantic stack end-to-end with mainstream tools.
  - ✅ `VectorStore` Protocol + in-proc default + **one** real adapter — shipped as **`sqlite-vec`** behind
    `[vector]` (pgvector + Qdrant have since shipped too, behind `[vector]`); plus sensitivity-gated persistence (`PersistencePolicy`).
  - ✅ `Extractor` registry + `Chunker` Protocol (formalized existing code; **zero behavior change** —
    `chunk_document`'s entry point/signature preserved, the PDF router untouched, all prior extraction/chunking
    tests stay green). See `extraction/registry.py` + `retrieval/chunking/chunker.py`.
  - ✅ The conformance kit for provenance + isolation + determinism, parametrized over registered backends
    (`tests/conformance/`, now binding both `InProcessVectorStore` and `sqlite-vec`). **Cross-seam provenance
    over `SourceConnector` now landed** (`test_source_connector_provenance.py`, parametrized over registered
    connectors + a lineage-dropping reverse-proof stub); the **`Chunker` provenance pack now landed too**
    (`test_chunker_provenance.py`, parametrized over registered chunkers + a lineage-dropping reverse-proof
    stub). The `Extractor` provenance pack (over real fixtures) + the privacy-trace pack remain open.
  - ✅ Registry + entry-point discovery so a backend is selectable by config string — config-string ✅
    (`make_vector_store` / `make_persistence_policy`) **and** entry-point auto-discovery ✅
    (`make_vector_store` falls back to the `ragspine.vector_stores` entry-point group, so a third-party
    `ragspine-foo` registers a backend by name with **no core PR** — user stories 1 & 4 land).
- **P1 — the breadth that wins evaluations.** Format coverage (DOCX/HTML/MD/CSV via `unstructured`/`docling`),
  rerank adapters (cross-encoder/Cohere/BGE), query-transform strategies (multi-query/HyDE/self-query),
  the first 2–3 `SourceConnector`s (**filesystem ✅ shipped** → S3 → HTTP/crawl). *(Vector adapters pgvector and Qdrant
  already shipped in P0/P1, and **native ANN/KNN with exact re-rank for all three has since shipped too**; the next
  vector adapter is Milvus — see the live contract [`vector-store.md`](../src/ragspine/retrieval/docs/vector-store.md).)*
- **P2 — governance & ops depth.** `FactStore` Protocol (DuckDB/Postgres), `TraceSink` → OTel (privacy-gated),
  incremental sync / deletion-propagation across stores (a 🛡 lineage concern), RAGAS-compatible eval export.

Each backend follows the ADR 0005 promotion rule: it earns "core/supported" status only when it has a real,
CI-tested path; until then it lives as a clearly-labeled experimental adapter.

## User stories

1. As a user, I set `vector_store = "qdrant"` in config and my pipeline uses Qdrant — without RAGSpine's core
   importing the Qdrant SDK, and without me writing glue.
2. As a user evaluating the library, `pip install ragspine` runs a full pipeline offline (BM25 + brute-force
   vector + Mock LLM) with zero heavy deps, so I can see it work before choosing backends.
3. As a contributor, I add `WeaviateVectorStore` in ~50 lines by implementing one `Protocol`; I run the shared
   conformance pack and it tells me immediately if I broke provenance or isolation.
4. As a third-party package author, I ship `ragspine-pinecone` that registers itself via entry points; users
   `pip install` it and select `"pinecone"` by string — no PR to RAGSpine required.
5. As a security-minded operator, I trust that *any* retriever/reranker in the registry — including ones I
   didn't write — cannot emit `RESTRICTED` content, because the conformance test enforces it for all of them.
6. As a maintainer, I see breadth as a bounded matrix with status, not an infinite backlog; commodity seams
   are adapters I review thinly, not code I own forever.
7. As a user with scanned PDFs / Notion / S3, there is a documented seam to plug each in, with an offline
   default so the absence of that backend never blocks the default loop.

## Implementation decisions

- **Uniform five-part contract** at every seam (Protocol · offline default · thin adapter · registry ·
  conformance). No seam invents its own extension style.
- **Adapters delegate, never re-implement.** A vector adapter wraps the store's client; it does not
  re-implement ANN. Keeps the maintenance surface thin and the dependency-license gate (ADR 0009) honest.
- **Lazy import inside the adapter, gated by an extra.** Core imports zero SDKs; importing `ragspine` never
  pulls a backend. Matches the existing `[pdf]/[ocr]/[llm]/[embed]/[service]` pattern; add `[vector]`,
  `[rerank]`.
- **Config-string + entry-point discovery (✅ shipped for `VectorStore`).** Backends are named; core resolves
  name→factory via a registry populated by built-ins **and third-party entry points** (`make_vector_store`
  resolves an unknown name through the `ragspine.vector_stores` entry-point group). No
  `if backend == "...": import ...` ladder in core — the built-in if-ladder became a lazy-loader registry, so a
  third-party package registers a backend by name with no core PR.
- **Conformance pack is the adapter spec.** It is written red first and is the authoritative definition of
  "a valid backend." Invariants are asserted *per registered implementation*, not once globally.
- **Offline defaults stay deterministic and dep-free**, preserving the BM25 + Mock default loop as the test
  and demo path (ADR 0005/0009).
- **Permissive-license-only for every adapter's deps** — the CI license gate (ADR 0009) extends to extras; an
  adapter pulling a GPL/SSPL dep is rejected.

## Testing decisions (TDD — write these red first)

- **Conformance: provenance.** A generic test runs every registered `SourceConnector`/`Extractor`/`Chunker`
  over a fixture and asserts every emitted unit has a non-null `source_doc_id` + locator. A deliberately
  lineage-dropping stub fails it.
- **Conformance: isolation.** Every registered `Retriever`/`Reranker` fed a `RESTRICTED` item returns output
  free of it. A stub that passes it through fails.
- **Conformance: privacy trace.** Every registered `TraceSink` rejects/scrubs a payload containing answer /
  fact value / chunk text.
- **Conformance: anti-fabrication.** With an adversarial always-fabricating `LLMProvider` and an empty
  structured channel, the orchestrator still answers "not found" (provider-independence regression).
- **Conformance: determinism.** Each offline default yields byte-identical output across two runs.
- **VectorStore behavior.** `upsert` then `query(k)` returns the k nearest with metadata-filter applied; the
  in-proc default and the real adapter pass the *same* test (parametrized).
- **Registry.** A backend registered by name is resolvable by config string; an unknown name errors clearly;
  a backend whose extra is uninstalled raises an actionable "pip install ragspine[vector]" message, not an
  `ImportError`.
- **Extractor registry.** A new mime type routes to its extractor without changing the router; an unregistered
  type yields a clear unsupported-format error.
- **Lean-default smoke.** With no extras installed, the full pipeline runs offline (extends the existing demo
  smoke), proving adapters are never on the default path.

## Out of scope (v1 of this PRD)

- **Authoring the long-tail connectors/stores ourselves.** The deliverable is the *contract + one or two
  reference adapters per seam*, not parity with LangChain's integration count. The rest is community/3rd-party.
- **A plugin marketplace / registry website.** Discovery is Python entry points + a docs table, not a hosted index.
- **Runtime auto-selection / cost-based routing across backends.** Backends are chosen by config, not inferred.
- **GraphRAG / knowledge-graph store.** A distinct quality-depth (⭐) effort — **now specified in its own PRD:
  [`prd-quality-depth.md`](prd-quality-depth.md) (workstream W7)**, with a charter-native deterministic structured
  relation graph (W7a) + an opt-in, provenance-bound narrative GraphRAG (W7b) behind a `GraphStore` seam (W7c).
- **Incremental sync / deletion-propagation engine** beyond the P2 seam stub — the lineage-correct delete is a
  meaty 🛡 design and gets its own PRD.
- **Migrating existing concrete backends to a worse abstraction.** Formalizing `Extractor`/`Chunker` is a
  no-behavior-change lift; if a seam has only one sensible impl forever, it need not become a Protocol.

## Further notes

- This PRD is the operational answer to "can a solo project be general-purpose?": **yes — by owning the spine
  and the quality stages, and renting the commodity surface through a contract that the conformance kit keeps
  honest.** The conformance kit is the part no breadth framework has, because none of them treats the
  invariants as code-enforced in the first place.
- The capability matrix is the canonical breadth backlog. Keep it current: a seam moves ✗→◐→✅ as its
  Protocol, default, adapter, and conformance land. When all seams are ✅, "general-purpose" is *demonstrated*
  (ADR 0003), not asserted.
- Pairs with the shipped pipeline-topology export (`HybridRetriever.topology()` + the git-ignored
  `docs/generated/topology*` renders): once backends are registry-selected, `.topology()` renders *which*
  backend each seam resolved to — the diagram tells the truth about the assembled stack, adapters included.
