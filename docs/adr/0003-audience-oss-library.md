---
status: accepted
date: 2026-06-17
---

# ADR 0003 — Audience: a general-purpose OSS library for others to build on

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction.

## Context

RAGSpine carried three conflicting audience signals at once: OSS-library (Apache 2.0,
PyPI name, "contributions welcome", Protocol extension points), internal enterprise
tool (RESTRICTED isolation, SME review queue, pervasive financial-reporting DNA), and
portfolio/reference showcase (fictional ACME demo, 943-test badge, "honest gaps"
roadmap). The audience was never stated, yet it gates every downstream decision.

## Decision

The **primary audience is other developers building their own RAG on top of
RAGSpine** — a general-purpose OSS library. Optimize for: a clean `Protocol`
extension surface, demonstrated (not asserted) generality, packaged CLIs, and
contributor-facing documentation. Finance becomes one example domain, not the identity.

## Alternatives considered (rejected)

- **Reference implementation / portfolio piece + OSS library** (the originally
  recommended, more conservative option): optimize for legible rigor and
  reproducibility, treat a contributor community as out of scope. Rejected — the user
  wants real external users, not a showcase.
- **Production internal enterprise tool**: wire in the dormant code, add auth, target
  a deployment. Rejected as the *primary* identity, though the codebase plausibly
  originated this way.

## Consequences

- "General-purpose" must be **proven**, forcing [0004](0004-domain-profile-generalization.md)
  (full generalization) and a second-domain example.
- Dormant/half-wired code becomes a credibility liability → [0005](0005-lean-core-experimental-isolation.md).
- The `service/` layer is reframed as a reference deployment example, not a product.
- Contributor surface matters: locale packs ([0007](0007-multilingual-architect-for-five-ship-two.md)),
  a "how to add a backend/locale" guide, CONTRIBUTING/governance.
