---
covers:
  - src/ragspine/retrieval/
verified-against: cab40fe
---

# retrieval — agent contract

Auto-loaded when working under `src/ragspine/retrieval/`. Keep terse; deep dives go
in `src/ragspine/retrieval/docs/`.

## What lives here

Narrative RAG. `chunking/` (paragraph-granular chunker + versioned store),
`lexical/` (Okapi BM25, CJK uni+bigram, RRF fusion), `vector/` (injectable
embedding backends, default none = pure BM25; + a pluggable `VectorStore` seam —
`store.py` — with an invariant-binding conformance kit in `tests/conformance/`),
`rerank/` (LLM listwise reranker, RRF-fallback), `link/` (adapter wiring
retrieval into the agent).

## Invariants

- **RESTRICTED isolation** — sensitivity-`RESTRICTED` content is stripped at two
  exits, `link/` and `rerank/`, before it can reach a prompt. Both must stay.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
