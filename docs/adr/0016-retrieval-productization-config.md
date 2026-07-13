---
status: accepted
date: 2026-07-14
---

# ADR 0016 — Retrieval productization config: metadata filtering, multi-index routing, parent-child preset, economy mode (批次 2.2)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Constrained by
[0005](0005-lean-core-experimental-isolation.md) (new capability isolated from the byte-identical
default path) and [0010](0010-intent-parser-security-decoupling.md) (deterministic security /
isolation independent of any model output). Mirrors the seam meta-pattern of
[0015](0015-relation-extractor-provenance-markers.md).

## Context

We want to match the **product-facing surface** of Dify's dataset retrieval (metadata filtering,
multi-knowledge-base routing, parent-child segmentation, economy vs high-quality modes) while
keeping RAGSpine's three code-level invariants intact for **every** new mode: anti-fabrication,
source provenance, and RESTRICTED double-exit (`link/` + `rerank/`) isolation. The default,
offline, zero-network path must stay **byte-identical**, and each new mode must be bound by
parametrized conformance so it cannot silently regress an invariant.

## Decision

Four additions, all offline-deterministic, opt-in, default byte-identical, each following the
family seam pattern (Protocol + zero-dependency deterministic default + `make_*` factory + env
selector + parametrized conformance):

- **① Metadata filtering** (`retrieval/filtering/`). `MetadataFilter`/`FilterCondition`
  (`metadata_filter.py`): a deterministic pre-scoring stage with a minimal operator set
  (`eq/ne/in/nin/gt/gte/lt/lte/between`, string lexicographic comparison, missing-field → no-match)
  that **only narrows** candidates — `apply` always returns an order-preserving subsequence.
  Because it only removes candidates and the two exits still strip RESTRICTED, a filter can **never**
  surface RESTRICTED (even one that deliberately selects it). Wired via
  `HybridRetriever.search(metadata_filter=)` / `NarrativeIndex.retrieve(metadata_filter=)`, default
  `None` = byte-identical. The **automatic** variant (`automatic.py`) is an opt-in seam:
  `FilterExtractor` Protocol + `make_filter_extractor` (default `none` → `None`; offline
  `ControlledVocabFilterExtractor` opt-in; LLM extractor an opt-in adapter). Its output is *only* a
  `MetadataFilter` (structurally unable to reach the answer channel) and only narrows candidates.

- **② Multi-index / multi-route routing** (`retrieval/routing/`). `MultiIndexRetriever`
  (`multi_index.py`) implements the A-line `NarrativeRetriever` protocol: **(a)** fan-out across
  libraries then cross-library **RRF fusion** (reuses `lexical.rrf_fuse`), tagging every result with
  `library_id` so provenance keeps the library-origin dimension; **(b)** routed mode via a
  `LibraryRouter` seam + `make_library_router` (default `none` = fan-out all; deterministic
  `KeywordLibraryRouter` matches the query against library descriptions; LLM router opt-in), falling
  back to all libraries on zero overlap so routing never starves recall. Isolation is **inherited** —
  each library's base already stripped RESTRICTED at its exit, so the fusion layer can only pass a
  subset through.

- **③ Parent-child chunk preset** (`ParentChildChunker` in `chunking/domain_presets.py`, aliases
  `parent_child`/`small_to_big`). A thin `LayoutAwareChunker` subclass overriding **only** the new
  `_child_extra` hook to attach `window_text` = the parent section's full text and `parent_locator` =
  the section's **real** para-span locator. Precise child hit → deterministic expansion to parent
  context; provenance points at the true parent locator, never fabricated. The base gains one
  additive `Chunk.parent_locator` field (default `""`) and one overridable hook; default chunkers set
  neither, so their output is byte-identical. Rides the existing `CHUNKER_IMPLS` provenance
  conformance.

- **④ Economy mode** (`retrieval/mode.py`). `RetrievalMode` + `make_retrieval_mode`
  (`ServiceConfig.retrieval_mode`) wraps the existing pure-BM25 path (`embedding_backend=None`) as an
  explicit `economy` preset (**zero embedding cost** — assembly constructs no embedding backend or
  vector store), switchable against `hybrid`/`vector` on the same config surface. Default `auto` =
  hybrid (embedding assembled per `ServiceConfig.embedding`, byte-identical).

Every new mode is bound by parametrized conformance with a non-vacuous reverse-proof:
`tests/conformance/test_metadata_filter_invariants.py` (per-operator narrowing/determinism + a
widening stub that must fail), `test_multi_index_isolation.py` (per-router isolation + provenance +
a leaky base that must fail), `test_retrieval_mode_invariants.py` (per-mode isolation/provenance +
economy zero-embedding).

Principle: **product surface as opt-in seams beside the default path, never within it; every new
retrieval mode inherits the three invariants deterministically and is proven so by conformance.**

## Alternatives considered (rejected)

- **Let the automatic filter extractor emit anything beyond filter conditions** (e.g. rewrite the
  answer): would open a fabrication channel. The extractor's return type is confined to
  `MetadataFilter`.
- **Make metadata filtering able to add or reorder candidates**: a widening/reordering filter could
  smuggle RESTRICTED or perturb the byte-identical default. `apply` is strictly narrowing and
  order-preserving.
- **Strip RESTRICTED inside `MultiIndexRetriever`**: duplicates the exit and risks divergence.
  Isolation is inherited from each library's existing exit; the fusion layer never reads chunks.
- **Repoint parent-child at retrieval-time store expansion now**: the chunker seam isn't yet wired
  into narrative ingest and `parent_id`/`window_text` aren't persisted; the preset lands the
  small-to-big signal at chunk time (parent context + real parent locator) without a schema change,
  leaving store-level expansion as a follow-up.
- **A boolean `economy` flag instead of a named mode preset**: a named `RetrievalMode` + factory
  matches the family `make_*` seam pattern and reads as a first-class config switch.

## Consequences

- The retrieval layer gains a Dify-parity product surface (metadata filtering, multi-library
  routing, parent-child segmentation, economy/vector modes) while the default offline path stays
  byte-identical and zero-network.
- Anti-fabrication, provenance (including a new library-origin dimension), and RESTRICTED
  double-exit isolation hold for every new mode, enforced deterministically and bound by
  parametrized conformance rather than trusted.
- New artifacts: `retrieval/filtering/` (MetadataFilter + FilterExtractor seam), `retrieval/routing/`
  (MultiIndexRetriever + LibraryRouter seam), a `ParentChildChunker` preset + `Chunk.parent_locator`
  field + `_child_extra` hook, `retrieval/mode.py` (RetrievalMode + factory), a `ServiceConfig.retrieval_mode`
  knob, and three conformance packs.
