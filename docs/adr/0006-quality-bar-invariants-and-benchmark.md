---
status: accepted
date: 2026-06-17
---

# ADR 0006 — Quality bar: invariants as property tests, plus one real retrieval benchmark

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction.

## Context

All evaluation is currently synthetic self-consistency: the QA golden set (41 cases)
scores 1.0 against a hand-aligned synthetic KB; the retrieval A/B harness uses
lexical-hash gold its own docstring says only proves "the harness math, not real
recall"; no real LLM, real corpus, or real embedding model runs in CI. For a
general-purpose library, accuracy is inherently the user's-data-dependent — a leaderboard
score is not RAGSpine's to own.

## Decision

Define quality as **guarantees, not scores**:

1. **Primary — invariants as property tests.** Anti-fabrication never fabricates,
   provenance is always present, RESTRICTED never leaks, behavior is deterministic —
   harden these from a few agent-layer tests into exhaustive property tests. This is
   the reason to choose RAGSpine over a dependency-heavy framework.
2. **Plus one real retrieval benchmark.** The single claim that *is* RAGSpine's own —
   "hybrid retrieval + listwise rerank beats naive BM25" — is an engine property, not a
   user-data property. Back it with a real (non-synthetic) labeled retrieval benchmark
   reporting real numbers (Recall@k, MRR), run in a dedicated lane (not necessarily
   every-CI).

Domain accuracy benchmarking is explicitly punted to the user's data.

## Alternatives considered (rejected)

- **Real-data accuracy eval as a release gate (option A)**: real labeled sets + real
  models across all channels as the bar. Rejected — accuracy depends on the user's
  data/model, which the library can't own.
- **Synthetic CI gate + separately-published full benchmark (option B)**: rejected as
  the *frame* (quality centered on accuracy numbers), but its "publish one real
  benchmark" slice is adopted for the retrieval claim only.

## Consequences

- "done / 1.0" means *invariants proven*, not *accuracy on synthetic data*.
- The retrieval benchmark gates promotion of the real vector backend out of
  experimental ([0005](0005-lean-core-experimental-isolation.md)).
- Keeps the fast, offline, deterministic CI loop intact as a regression tripwire.
