---
status: accepted
date: 2026-06-17
---

# ADR 0010 — Intent parsing: deterministic security gate + pluggable IntentParser

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Forced by [0004](0004-domain-profile-generalization.md) and [0007](0007-multilingual-architect-for-five-ship-two.md).

## Context

The intent parser is rule-based, zero-LLM, and finance/Chinese-tuned (synonym matching,
hardcoded `_SUPPORTED_METRICS`, inline `_CHANNEL_SYNONYMS`). It is also the
**security-critical front door**: out-of-scope / competitor refusal is deterministic and
happens *before* any LLM or retrieval call, and external-entity masking lives here. Two
prior decisions break a pure rule parser: full generality ([0004](0004-domain-profile-generalization.md))
means arbitrary user-defined dimensions a finance-tuned parser can't read, and
multilingual ([0007](0007-multilingual-architect-for-five-ship-two.md)) makes
per-language synonym tables a maintenance explosion.

The key insight: **the security decision and the intent decision are separable.**
"Is this entity out-of-scope / competitor / RESTRICTED?" is security-critical and must
stay deterministic. "Which dimension/period is being asked?" is not — a mis-parse there
only yields a clarification or not-found, never a leak.

## Decision

**Decouple them.**

- The **security gate** (out-of-scope / competitor / RESTRICTED) becomes an
  independent, always-on, **never-pluggable** deterministic guard that runs on the
  parser's output and is declared via `DomainProfile`. It never consults an LLM.
- **Intent extraction** becomes an **`IntentParser` Protocol** with a default zero-LLM,
  config-driven rule implementation (the offline default per
  [0009](0009-dependency-and-framework-policy.md)) and an optional LLM-classifier
  backend (an extra) for robust multilingual / arbitrary-domain parsing.

Principle: **deterministic where it matters (security), flexible where it's safe (intent).**

## Alternatives considered (rejected)

- **Pure rule parser, config-driven, no LLM ever (option A)**: maximal purity, but
  forces every user to hand-author per-domain/per-locale synonym config — brittle for
  morphologically rich languages and high adoption friction.
- **LLM classifier as the primary parser (option C)**: most flexible, but makes the
  front door LLM-dependent and risks a mis-parse defeating the security gate; loses the
  zero-LLM offline default.

## Consequences

- The security guarantee strengthens: it is now an explicit, isolated, non-pluggable
  component rather than an emergent property of "the rule parser happens to be deterministic."
- Preserves the zero-LLM offline default while giving generality/multilingual an opt-in path.
- New artifacts: a `SecurityGate` component and an `IntentParser` Protocol with two
  implementations.
