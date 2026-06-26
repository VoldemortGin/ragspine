# graph — agent contract

Auto-loaded when working under `src/ragspine/graph/`. Keep terse; deep dives go in
`src/ragspine/graph/docs/` (not written yet). References name **symbols**, not line numbers.

## What lives here

GraphRAG (W7), charter-aligned: two layers + one seam, all **new opt-in capability** — the
default `answer_question` / retrieval / eval path stays byte-identical (ADR 0001/0005).

- `store.py` — **W7c `GraphStore` seam** (the breadth contract). A `@runtime_checkable`
  `GraphStore` Protocol (`upsert_nodes/edges`, `get_node`, `neighbors`, `subgraph`,
  `traverse`, `count_*`) + the zero-dep deterministic default `InProcessGraphStore` +
  `make_graph_store` / `RAGSPINE_GRAPH_STORE` registry (built-ins `in_process` + `networkx`;
  third-party via the `ragspine.graph_stores` entry-point group). Five-段式范式同
  `make_vector_store`. `GraphNode` / `GraphEdge` / `Subgraph` are frozen, lineage-carrying.
- `relation.py` — **W7a deterministic structural graph**. `build_relation_graph(profile, *,
  facts, chunks, store=None)` builds a typed graph over the **already-controlled dimensions**
  — `parent_of` (home→subsidiaries), `derives` (any dim's `derived_from`/`derivation`),
  `competes_with` (home→external entities), `mentions` (doc→entity/metric co-occurrence from
  facts + chunks). Zero LLM, zero fabrication, every node/edge carries provenance.
- `query.py` — **W7a multi-hop query entry** (standalone, opt-in; never touches `agent.py`).
  `GraphQuery.subsidiary_rollup` / `peer_comparison` / `derivation_trace` — the multi-hop a
  flat top-k + exact SQL can't do, fully cited. Inherits the `SecurityGate` (competitor
  refusal) + the store's RESTRICTED isolation.
- `narrative.py` — **W7b narrative GraphRAG skeleton** (opt-in, default-off, behind
  `[graph]`+`[llm]`). `GraphExtractor` / `LLMGraphExtractor`, deterministic
  `detect_communities`, `CommunitySummarizer` / `LLMCommunitySummarizer`,
  `make_narrative_graph` / `RAGSPINE_NARRATIVE_GRAPH`. LLM extraction is non-deterministic →
  never on the default path; community summaries are **syntheses, never citable facts**.
- `adapters/` — third-party `GraphStore` adapters (`networkx_store.py`), lazy-imported,
  behind `[graph]` extra; each runs the same `tests/conformance/test_graph_store.py` pack.

## Invariants (do not break)

- **RESTRICTED isolation (a new exit, inherited).** Graph traversal is a new path that could
  reach a prompt, so the `GraphStore` strips it: a `sensitivity == RESTRICTED` node never
  surfaces in `get_node` / `neighbors` / `subgraph` / `traverse`, and never acts as a
  multi-hop stepping-stone (edges touching it never appear). Same rule as the
  `retrieval/link` + `retrieval/rerank` two exits; GraphRAG **must not** bypass it. Frozen by
  `tests/conformance/test_graph_store.py` (incl. a `_LeakyGraphStore` reverse-proof that must
  FAIL). Doc nodes inherit `sensitivity` from their source chunk (most-restrictive wins).
- **Competitor / out-of-scope refusal (inherited).** `GraphQuery` screens every requested
  entity through the deterministic `SecurityGate` **first**; a competitor/external entity is
  refused (`refused=True`, zero data) — graph queries cannot smuggle competitor data.
- **Provenance.** Every `GraphNode` / `GraphEdge` carries non-empty `source_doc_id` +
  `source_locator`; profile-derived edges record the controlled config as source, doc
  co-occurrence edges carry the fact/chunk lineage. A lineage-dropping stub fails the pack.
- **Determinism (W7a + in-process default).** Same input → byte-identical graph + traversal
  (sorted construction, sorted BFS frontier, sorted output). W7b's LLM extraction is the only
  non-deterministic piece and is opt-in/default-off; its **community detection** is still
  deterministic (connected-components).
- **Anti-fabrication unbroken.** Numbers stay in the structured channel. The relation graph
  never invents facts; W7b community summaries are explicitly `is_synthesis=True` and never
  citable as facts. `answer_question` is untouched — W7 adds capability beside it, not within.

## Read before editing

- **`store.py` is the fixed spine — keep the three invariants implementation-enforced**, not
  commented. The `networkx` adapter must replicate `InProcessGraphStore`'s isolation/`where`/
  sort semantics exactly (it reuses `_node_matches` + the restricted check) so it passes the
  same conformance pack. Don't add backend knobs to the core Protocol.
- **`GraphQuery` is standalone and opt-in.** Do not thread it into `answer_question` — the
  default loop must stay byte-identical. It is a new public capability beside the default path.
- **`make_graph_store` / `make_narrative_graph` default to off** (`None`/`'none'` → `None`).
  Keep that contract; the registry idiom mirrors `make_vector_store` / `make_corrective_retriever`.

## Deep dives

Planned (`src/ragspine/graph/docs/`, not written yet):

- the `GraphStore` seam — Protocol, in-process default, networkx adapter, conformance pack
  (provenance / isolation / determinism + the two reverse-proofs), at-rest honesty.
- W7a structural relation graph — the controlled-dimension edge types + the multi-hop query
  semantics (subsidiary roll-up / peer comparison / derivation trace) and their citability.
- W7b narrative GraphRAG — the extract → community → summary skeleton, the LLM degrade
  discipline, and the follow-ups (Leiden/Louvain, incremental, claim-anchoring, global query).
