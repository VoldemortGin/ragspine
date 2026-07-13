---
status: accepted
date: 2026-07-13
---

# ADR 0015 — Relation extraction: opt-in slot with model-derived/unverified provenance markers

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Constrained by
[0005](0005-lean-core-experimental-isolation.md) (experimental capability isolated from the
default path) and [0010](0010-intent-parser-security-decoupling.md) (deterministic security gate).

## Context

The W7a structural relation graph (`build_relation_graph`) is fully deterministic and
citable: every edge derives from a controlled dimension (profile config, facts, chunks).
But some useful relations live only in narrative text (`A partners with B`), reachable only
by an LLM — which is non-deterministic and can fabricate. We want that capability without
letting a model-asserted edge masquerade as a controlled, verified fact, and without
touching the byte-identical default path.

The key insight (same shape as [0010](0010-intent-parser-security-decoupling.md)): a
model-extracted relation is *useful but untrusted*. It must be **labelled** as such, and the
two hard guarantees — RESTRICTED isolation and competitor refusal — must hold on this new
input path too, deterministically, regardless of what the model returns.

## Decision

Add a `RelationExtractor` **Protocol slot** (`extractor.py`), consumed by
`build_relation_graph(..., relation_extractor=None)`:

- The default is `None` → the base graph is **byte-identical** to before (no extra edges).
- The default *extractor* (when one is wanted) is `DeterministicRelationExtractor`: rule-based
  same-document entity co-occurrence (`co_occurs_with`), zero LLM, deterministic, with **clean
  lineage** (doc source only, no model markers) — genuinely distinct from the base doc→entity
  `mentions` edges.
- `LLMRelationExtractor` is **opt-in** (behind `[llm]`) and mirrors `LLMGraphExtractor`'s
  degrade discipline. Every edge it emits is stamped `derived=model-derived` +
  `verified=unverified` (never silently trusted); its lineage is stamped from the **chunk
  (caller)**, never the model's self-report; RESTRICTED chunks **never reach the LLM**; and
  **both endpoints of every relation are screened through the deterministic `SecurityGate`** —
  a competitor/external endpoint drops the edge. It degrades to nothing on `ProviderError` /
  bad JSON, and is bounded by `max_relations`.
- Selection mirrors `make_narrative_graph`: `make_relation_extractor(spec, *, provider, profile)`
  / `RAGSPINE_RELATION_EXTRACTOR` (`none` → `None`; `deterministic`/`rule`/`cooccurrence`;
  `llm`/`on` → honest degrade to `None` without a provider).

Principle: **capability beside the default path, never within; model-asserted edges are
labelled and screened, never silently adopted.**

## Alternatives considered (rejected)

- **Materialize LLM edges with the same clean lineage as controlled edges**: would let a
  fabricated relation read as a verified fact — defeats the provenance invariant.
- **Skip the SecurityGate on extracted edges** (trust the base graph's node isolation): a
  model could name a competitor never present as a node, smuggling a competitor relation in.
  Screen at extraction time instead.
- **Wire extraction into the default `build_relation_graph` unconditionally**: breaks the
  byte-identical default and puts a non-deterministic step on the controlled path.

## Consequences

- The relation graph gains an opt-in narrative-relation capability while the default path
  stays byte-identical and every model-derived edge is self-labelling and auditable.
- The two hard guarantees (RESTRICTED isolation, competitor refusal) now hold on the new
  extraction input path, enforced deterministically rather than trusted from the model.
- New artifacts: a `RelationExtractor` Protocol, a deterministic default and an LLM
  implementation, a factory/env selector, and provenance-marker constants.
