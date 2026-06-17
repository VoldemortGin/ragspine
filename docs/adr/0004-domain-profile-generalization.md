---
status: accepted
date: 2026-06-17
---

# ADR 0004 — Full generality: DomainProfile and arbitrary-dimension facts

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Gated by [0003](0003-audience-oss-library.md).

## Context

The narrative RAG + agent + anti-fabrication spine is genuinely domain-agnostic, but
the structured channel is finance to the bone: a fixed `metric/entity/period/channel`
4-tuple, `fact_metric` tables, REVENUE/PROFIT/ROE, and a metric vocabulary still
hardcoded in `common/glossary.py` (entities/geography/sensitivity are already
config-driven; metrics are not). Having chosen a general-purpose library
([0003](0003-audience-oss-library.md)), "general" must be true of *both* channels.

## Decision

Generalize **fully**. `CompanyProfile` → `DomainProfile`. The structured channel
becomes a **typed-fact store with arbitrary user-defined dimensions** plus a parametric
query over them; metric/period/channel collapse into declared dimensions. The metric
vocabulary and synonyms move from `glossary.py` into config. Finance/ACME ships as
**one bundled example among ≥2**, with at least one non-financial example to prove
generality by demonstration.

## Alternatives considered (rejected)

- **Spine-only generality (option B)**: declare only narrative RAG + agent +
  anti-fabrication as "general"; keep the structured numeric channel as a forkable
  finance module. Rejected — it surrenders the rarest differentiator (deterministic
  structured numeric Q&A) to "fork it yourself."
- **Config-driven-but-no-second-domain (option C)**: prove generality by construction
  (no hardcoded finance) without yet shipping a second-domain demo. Rejected as the
  *endpoint* — chosen full generalization instead, with a real second-domain example.

## Consequences

- Largest engineering bet in the set: schema change (4-tuple → arbitrary dimensions),
  dynamic tool-schema generation, and config-driven parsing all follow.
- Subsumes the standalone "config-driven completeness" question.
- Forces the intent parser to stop hardcoding finance dimensions → [0010](0010-intent-parser-security-decoupling.md).
- **Risk:** the rigor came partly *from* the finance constraints; generalization must
  not weaken any invariant. This was the user's more-ambitious override of the
  recommended incremental path — implementation must guard the invariants explicitly.
