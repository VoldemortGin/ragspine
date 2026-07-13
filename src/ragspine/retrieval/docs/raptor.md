---
covers:
  - src/ragspine/retrieval/raptor.py
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# RAPTOR — recursive-cluster multi-granularity tree (W10)

Deep dive behind the **W10** row of [`docs/prd-quality-depth.md`](../../../../docs/prd-quality-depth.md)
(P2, ⭐). Benchmarks **LlamaIndex RAPTOR pack / RAGFlow RAPTOR** (Sarthi et al. 2024). RAPTOR is the
**second mainstream global-synthesis route**, parallel to W7b narrative GraphRAG: leaves are chunks,
each level **clusters the level's node vectors** and **summarizes each cluster** into a higher node, so
the tree spans fine detail (leaves) → broad theme (internal nodes). Retrieval can hit a leaf **or** an
internal summary, filling the global / multi-hop synthesis gap a flat top-k can't.

**All opt-in, default-off.** `build_raptor_tree` / `RaptorTree.retrieve` are a *standalone new
capability* (the W7a `GraphQuery` idiom — never threaded into `answer_question`); `RaptorRetriever` is an
optional `NarrativeRetriever` wrapper and `make_raptor_retriever(base, "none")` returns `base` unchanged.
No default-path file changed → the default loop / retrieval / eval stay **byte-identical**.

## The tree

`build_raptor_tree(chunks, *, embedder, summarizer=None, threshold=…, max_levels=3, min_cluster_size=2)`:

- **Isolation at the door.** `sensitivity == RESTRICTED` chunks are dropped **before** any node is
  built, so RESTRICTED can never enter a leaf, a summary, or a provenance list. Frozen by
  `test_restricted_never_enters_tree` + an honest **reverse-proof** (`test_restricted_exclusion_reverse_proof`:
  the same chunk marked `INTERNAL` *does* enter — proving the exclusion is the isolation check, not a
  coincidence of the text being absent).
- **Leaves** (`level 0`, `is_synthesis=False`) wrap each visible chunk, carrying its `doc_id` /
  `source_locator` and its embedding (`embedder.embed_texts`). With `embedder=None` the tree is
  **leaves-only** — an honest degrade (no clustering possible), not a crash.
- **Recurse up.** Each level clusters the current nodes' vectors with **deterministic threshold
  clustering** (`cluster_by_similarity`: cosine `≥ threshold` builds an edge, connected components via
  union-find — the exact W7b `detect_communities` idiom, **zero randomness**, members + clusters sorted).
  Each cluster `≥ min_cluster_size` becomes a summary node; singletons carry forward. The loop is
  **bounded** (`max_levels`; stops when a level can't merge or stops shrinking) so it always terminates.
- **Summaries are syntheses.** Every internal node is `is_synthesis=True` — an **explicitly labelled
  synthesis, never citable as a fact** (the W5 / W7b anti-fabrication discipline). Numbers still route
  through the structured channel; the LLM summarizer prompt forbids concrete numbers, mirroring W7b.
- **Provenance, never fabricated.** Each summary records the **union of its members' `source_doc_id` /
  `source_locator`** (sorted), and that union is `⊆` the leaf provenance — frozen by
  `test_summary_provenance_never_fabricated`.

Same chunks + same embedder ⇒ **byte-identical tree** (`test_build_tree_is_deterministic`): sorted
clustering, sorted provenance unions, deterministic summaries.

## Summarizer seam

`RaptorSummarizer` Protocol (`summarize(texts) -> str`), two implementations:

- **`ExtractiveRaptorSummarizer`** — the **deterministic, zero-LLM default**: a per-member leading
  excerpt join. Offline + byte-reproducible, so a determinism-only deployment still gets a multi-
  granularity tree (this also lands the PRD's "deterministic extractive cluster-summary" follow-up).
- **`LLMRaptorSummarizer(provider)`** — **opt-in behind `[llm]`**: a theme synthesis via the provider
  (numbers forbidden); on `ProviderError` / empty reply it **degrades to the extractive fallback** —
  never crashes, never fabricates. Frozen by `test_llm_summarizer_degrades_to_extractive`.

## Multi-granularity retrieval

`RaptorTree.retrieve(query, embedder, *, top_k, granularity)` is **collapsed-tree** retrieval: score
the selected nodes by query cosine, deterministic sort (`-score, node_id`). `granularity` = `"all"`
(leaves + summaries), `"leaves"` (detail only), or `"summaries"` (theme only).

`RaptorRetriever` (opt-in `NarrativeRetriever` wrapper) returns the **base leaf snippets first** (real,
citable chunks, already RESTRICTED-stripped at the `link/` exit) then appends the top summary hits as
snippets **tagged `is_synthesis=True`** with honest multi-source provenance (`⊆` real leaf lineage) —
global/thematic context that downstream can distinguish from citable fact. **Isolation is inherited
twice**: the base already stripped RESTRICTED, and the tree was built without it.

## Config selection (mirrors `make_narrative_graph` / `make_corrective_retriever`)

- `make_raptor_summarizer(spec, *, provider)` — `none`→`None`, `extractive`/`deterministic`→extractive,
  `llm`/`on`→ provider-gated LLM (no provider ⇒ `None`), env `RAGSPINE_RAPTOR_SUMMARIZER`.
- `make_raptor_retriever(base, spec, *, tree, embedder)` — `none`→`base` (byte-identical),
  `raptor`/`on`→`RaptorRetriever` (missing `tree`/`embedder` ⇒ `base`, honest degrade), env
  `RAGSPINE_RAPTOR`.

## Follow-up (honest boundaries)

collapsed-tree vs tree-traversal retrieval-mode A/B; incremental tree updates on re-ingest; a
UMAP+GMM clustering **opt-in adapter** (the paper's soft clustering, deliberately *not* the default —
it introduces randomness); safely threading synthesis nodes into `answer_question` behind a citation-
suppression path; a global-synthesis golden A/B against W7b.
