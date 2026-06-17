---
status: accepted
date: 2026-06-17
---

# ADR 0002 — Product direction: a general-purpose, license-clean, framework-unlocked RAG library

> Immutable record. ADRs are exempt from drift tracking (no `covers`). To reverse a
> decision, add a new ADR that supersedes the old one rather than editing it.

This is the umbrella ADR. It records the north star and indexes eight per-decision
ADRs (0003–0010) settled in a structured design interview on 2026-06-17.

## Context

RAGSpine had strong, code-enforced rigor (anti-fabrication, provenance, RESTRICTED
isolation, privacy-aware traces) but an unchosen strategy: audience, scope, and the
finance-vs-general bet were all undecided, and a large body of capability was built
but not wired into the running pipeline. We resolved the strategy top-down,
foundational forks first.

## Decision

**North star:** a deterministic, license-clean, framework-unlocked RAG spine — a
library you assemble in plain Python, where anti-fabrication / provenance /
source-isolation are **code-enforced invariants, not prompt suggestions**;
domain-agnostic by config, multilingual by locale-pack, every backend swappable via
`Protocol`.

**The through-line across all eight decisions:** separate *guarantees* from
*flexibility*. Invariants (anti-fabrication, provenance, isolation, dependency
licensing, the security gate) are hard, deterministic, and non-pluggable. Everything
else (domain, language, LLM, embedding, reranker, parsing strategy) is soft —
config- or Protocol-driven.

| # | Decision | ADR |
|---|---|---|
| 1 | Audience = general-purpose **OSS library** for others to build on | [0003](0003-audience-oss-library.md) |
| 2 | **Full generality** via `DomainProfile`; finance is one bundled example | [0004](0004-domain-profile-generalization.md) |
| 3 | **Lean core + experimental/extras isolation**; no dormant code in core | [0005](0005-lean-core-experimental-isolation.md) |
| 4 | Quality = **invariants as property tests** + one real retrieval benchmark | [0006](0006-quality-bar-invariants-and-benchmark.md) |
| 5 | Multilingual = **architect-for-5, ship-2** (Chinese + English), locale packs | [0007](0007-multilingual-architect-for-five-ship-two.md) |
| 6 | Prompts = packaged `ragspine/prompts/<locale>/` behind a `PromptRegistry` | [0008](0008-prompt-registry-packaging.md) |
| 7 | framework-free = **no framework lock-in + permissive-license-only**; lean default | [0009](0009-dependency-and-framework-policy.md) |
| 8 | Intent parsing = **deterministic security gate** + pluggable `IntentParser` | [0010](0010-intent-parser-security-decoupling.md) |

## Consequences

Near-term workstreams that fall out (no further decision needed):

- `CompanyProfile` → `DomainProfile`; move the hardcoded metric vocabulary out of
  `common/glossary.py` into config.
- Structured channel schema: `fact_metric` 4-tuple → arbitrary user-defined dimensions.
- Extract a deterministic `SecurityGate` from the intent parser; make `IntentParser` a Protocol.
- `PromptRegistry` + `ragspine/prompts/<locale>/`.
- Locale seam: tokenizer registry + message catalog + `LocaleProfile`.
- CI: dependency-license gate; wire up `scripts/check_doc_drift.py`.
- One non-financial second-domain example + one real retrieval benchmark (turn
  "general" and "hybrid > BM25" from assertion into evidence).
- Rewrite README / positioning language.

**Derived positions (not separately interviewed; flag if wrong):**

- The FastAPI + RQ `service/` layer is a **reference deployment example**, not a core
  guarantee (follows from 0003 + 0005). Its missing auth is therefore not a near-term
  priority unless we promote the service to a first-class deliverable.
- Full config-driven behavior is subsumed by `DomainProfile` (0004).

**Risks:**

- Full generalization (0004) is the heaviest bet — a real refactor with a risk of
  diluting the rigor that came *from* the original finance constraints. Implementation
  must hold the line: generalize without weakening any invariant.
- The eight decisions together describe a large surface for a fresh single-author
  v0.1. Sequence the work as "make the lean core credible first, then promote
  experimental modules one at a time," not all at once.
