---
covers:
  - src/ragspine/agent/agent.py
  - src/ragspine/retrieval/link/
  - src/ragspine/retrieval/rerank/
  - src/ragspine/common/observability/
verified-against: a9f5b31
---

# Invariants (code-enforced)

Authoritative detail for the invariants summarized in the root `CLAUDE.md`. Each
entry should name: what it guarantees · where it is enforced · the test that
freezes it.

## Anti-fabrication

<!-- TODO: where the rewrite happens (src/ragspine/agent/agent.py) and the regression test. -->

## Provenance

<!-- TODO: source_doc_id + locator carried end to end; where lineage could be dropped. -->

## RESTRICTED isolation (two exits)

**Guarantees** `sensitivity == RESTRICTED` content never reaches an LLM prompt or an answer.
**Enforced** at two exits before any prompt: `retrieval/link/narrative_link.py` (the snippet
adapter drops RESTRICTED chunks) and `retrieval/rerank/listwise_rerank.py` (RESTRICTED text never
enters the listwise judge prompt). Both must stay; neither is sufficient alone.

**Judge-agnostic (W2).** The rerank exit's protection lives in the `listwise_rerank` *orchestration*,
not in any particular judge, so it covers **every** `ListwiseJudge` equally: the LLM listwise judge
(`ProviderListwiseJudge`) and the offline local cross-encoder (`retrieval/rerank/cross_encoder.py`,
`CrossEncoderReranker`) both receive only the non-RESTRICTED subset. Adding the cross-encoder seam
therefore inherits — and cannot bypass — the two-exit rule. **Frozen by**
`tests/retrieval/rerank/test_cross_encoder_isolation.py` (RESTRICTED never reaches the cross-encoder;
a reverse-proof shows the assertion has teeth — the same reranker *does* score the text when handed
it directly, bypassing the seam).

**At-rest (third, persistence layer).** Persisting a chunk's embedding writes a *recoverable
derivative* of its text next to its lineage (`doc_id`, `source_locator`) — a surface that bypasses
both exits. The swappable `PersistencePolicy`
(`src/ragspine/retrieval/vector/persistence_policy.py`) gates this: the default `IsolationFirstPolicy`
**never persists a RESTRICTED chunk's vector at rest** (it still retrieves via BM25 with vector
score 0, then is stripped at the two exits). `PersistEverythingPolicy` is opt-in and **only**
appropriate when the entire vector store is itself classified RESTRICTED-tier at rest — encrypted
volume, access-controlled, and excluded from routine backups. The `where`-filter pushdown in
`VectorStore` (`retrieval/vector/store.py`) is an additional optional enforcement point at the store.

**Frozen by** `tests/retrieval/lexical/test_persistence_ingest.py` (default policy persists zero
RESTRICTED vectors; opt-in persists them) and the existing two-exit tests under
`tests/retrieval/link/` and `tests/retrieval/rerank/`.

**Inherited by the W6 opt-in agentic paths (decomposition / CRAG / multi-turn).** All three W6 features are
opt-in, default-off, and re-use the existing exits rather than opening a new one: W6a query decomposition
(`agent/decompose.py`) re-runs the **full** `answer_question` per sub-question, so the security gate + two
exits screen every sub-question independently (a competitor sub-question is still out-of-scope-refused); W6b
corrective retrieval (`retrieval/corrective.py`, `CorrectiveRetriever`) only ever returns a *subset* of its
wrapped base retriever's already-RESTRICTED-stripped output and never reads chunks directly (frozen by
`tests/retrieval/corrective/test_corrective_isolation.py` + reverse-proof); W6c conversational memory
(`service/conversation.py`) re-screens the augmented question through the gate every turn and never carries
home context into an out-of-scope question or remembers a refused turn (frozen by
`tests/service/test_conversation.py`). None can leak RESTRICTED / bypass competitor refusal by construction.

**Inherited by the W7 GraphRAG paths (graph traversal is a new exit).** Graph traversal is a new path that could
reach a prompt, so the `GraphStore` (`src/ragspine/graph/store.py`) screens it like the two exits: a
`sensitivity == RESTRICTED` node never surfaces in `get_node` / `neighbors` / `subgraph` / `traverse`, and never
acts as a multi-hop stepping-stone (edges touching it never appear); doc nodes inherit `sensitivity` from their
source chunk (most-restrictive wins). The W7a multi-hop `GraphQuery` (`graph/query.py`) additionally screens every
requested entity through the deterministic `SecurityGate` **first**, so a competitor/external entity is refused
with zero data — graph queries cannot smuggle competitor data or RESTRICTED content. **Frozen by**
`tests/conformance/test_graph_store.py` (provenance / RESTRICTED-never-surfaces / determinism, each with an honest
reverse-proof stub — `_LeakyGraphStore` / `_LineageDroppingGraphStore` — that must FAIL) and the W7a/W7b tests
under `tests/graph/`. The authoritative per-domain contract is `src/ragspine/graph/CLAUDE.md`.

**Inherited by the W8 post-retrieval postprocessor chain (opt-in, no new exit).** The W8 chain
(`retrieval/postprocess.py`: `MMRPostprocessor` / `LostInTheMiddlePostprocessor` / `CompressionPostprocessor`,
composed by `make_postprocessor`) runs *inside* `NarrativeIndexRetriever.retrieve` **after** the `link/` exit has
already stripped `sensitivity == RESTRICTED`, and only ever reorders / de-dups / compresses that already-stripped
subset — it never reads chunks directly and never fabricates a snippet, so RESTRICTED can neither enter a
processor nor surface (the W6b `CorrectiveRetriever` idiom). Default `"none"` ⇒ no chain ⇒ retrieval output is
byte-identical. Compression preserves provenance by writing a separate `prompt_text` key (`agent._snippet_text`
prefers it for the prompt) while leaving the original `text` + every reference field (`source_locator` / `doc_id`
/ `chunk_id`) untouched — the W4a index_text layering. **Frozen by**
`tests/retrieval/postprocess/test_postprocess_isolation.py` (real-index integration: a RESTRICTED chunk never
surfaces through the `mmr,lost_in_middle,compress` chain, with a **reverse-proof** that a RESTRICTED snippet fed
*directly* passes through — proving the protection lives at the upstream exit, not the postprocessor). The
authoritative per-domain contract is `src/ragspine/retrieval/docs/postprocess.md`.

**Inherited by the W9 query transforms (opt-in, no new exit).** The W9 LLM query transforms
(`agent/query_transform.py`: `HyDERetriever` / `RAGFusionRetriever` / `StepBackRetriever`, composed by
`make_query_transform`) are `NarrativeRetriever` wrappers (the W6b `CorrectiveRetriever` idiom): each only ever
calls `base.retrieve(...)` — whose `link/` exit has already stripped `sensitivity == RESTRICTED` — and only
reorders / fuses (via `rrf_fuse`) that already-stripped subset, so RESTRICTED can neither enter a transform nor
surface. Two further guarantees make the transforms anti-fabrication-safe: (1) **HyDE's hypothetical document is
a retrieval probe, never a citable fact** — it replaces only the *query text* fed to `base.retrieve`; the
returned snippets are the real chunks with real lineage, and the hypothetical doc never enters a snippet /
answer / citation. (2) **Every LLM-generated variant / step-back question re-runs the deterministic
`SecurityGate` before retrieval** — a competitor / out-of-scope generated query is dropped and never retrieved
(the W6a idiom; the original question already passed the gate at `answer_question` entry, so this screens only
the *newly introduced* queries). Default `"none"` ⇒ `make_query_transform` returns the base unchanged ⇒ the
retriever path is byte-identical (and `answer_question` itself is untouched — Adaptive-RAG reuses the existing
`decomposer=` seam via `AdaptiveDecomposer`, `multi` → W6a fan-out, `simple`/`single` → the byte-identical
single-shot route). **Frozen by** `tests/agent/test_query_transform.py` (HyDE probe-never-a-fact; RAG-Fusion RRF
fusion; per-variant / per-step-back competitor screening with a spy-base **reverse-proof**; a real-index
isolation integration test with a RESTRICTED-in-store reverse-proof; factory byte-identity). The authoritative
per-domain contract is `src/ragspine/agent/CLAUDE.md`.

## Privacy-aware traces

<!-- TODO: common/observability records codes / counts / timings only. -->
