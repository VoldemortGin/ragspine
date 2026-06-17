---
covers:
  - ragspine/agent/agent.py
  - ragspine/retrieval/link/
  - ragspine/retrieval/rerank/
  - ragspine/common/observability/
verified-against: cce8730
---

# Invariants (code-enforced)

Authoritative detail for the invariants summarized in the root `CLAUDE.md`. Each
entry should name: what it guarantees · where it is enforced · the test that
freezes it.

## Anti-fabrication

<!-- TODO: where the rewrite happens (ragspine/agent/agent.py) and the regression test. -->

## Provenance

<!-- TODO: source_doc_id + locator carried end to end; where lineage could be dropped. -->

## RESTRICTED isolation (two exits)

<!-- TODO: the two filter points — retrieval/link and retrieval/rerank. -->

## Privacy-aware traces

<!-- TODO: common/observability records codes / counts / timings only. -->
