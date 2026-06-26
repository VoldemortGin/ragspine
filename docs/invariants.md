---
covers:
  - src/ragspine/agent/agent.py
  - src/ragspine/retrieval/link/
  - src/ragspine/retrieval/rerank/
  - src/ragspine/common/observability/
verified-against: 9242275
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

## Privacy-aware traces

<!-- TODO: common/observability records codes / counts / timings only. -->
