---
status: accepted
date: 2026-06-17
---

# ADR 0009 — "Framework-free" redefined: no framework lock-in + permissive-license-only

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction.

## Context

The headline "framework-free / zero-SDK-core / runs offline" mis-describes the actual
constraint. The real requirements are: (a) **no LangChain-family orchestration lock-in**
(LangChain / LlamaIndex / Dify / LangGraph), and (b) **dependency license hygiene** —
nothing stricter than Apache-2.0 (exclude GPL/AGPL/LGPL, SSPL, non-commercial, etc.).
Mature foundational libraries (torch, numpy, sentence-transformers) are acceptable.
"Zero dependencies" was never the point.

## Decision

Redefine **framework-free = (a) no orchestration-framework lock-in + (b)
permissive-license-only (≤ Apache-2.0 permissiveness)**. Concretely:

- **Positioning rewrite:** from "zero-SDK / runs offline" to "no framework lock-in +
  license-clean + every backend swappable via `Protocol`."
- **Dependency-license gate in CI:** every transitive dependency must be permissively
  licensed; the gate fails otherwise. This operationalizes the constraint and is itself
  a selling point for compliance-minded users.
- **Lean default install:** real backends (torch/embeddings) are opt-in via extras
  (`[embed]`, etc.) — a packaging/UX choice about install weight and optional GPU, no
  longer a purity choice. The offline-deterministic path (BM25 + MockProvider) remains
  the default test/demo loop, a kept strength rather than a dogma.

## Alternatives considered (rejected)

- **Absolute zero-SDK core, all real backends in extras for purity (option A)**:
  rejected as the *rationale* — torch/numpy are licensed-and-lock-in acceptable, so
  purity is not the reason; install-weight is. (The lean-default outcome is kept, for a
  different reason.)
- **Batteries-included default (ship a real CPU embedding in the base install)**:
  rejected — a multi-GB default is an adoption barrier for a library under evaluation.
- **Tiered `ragspine-core` + `ragspine` distributions**: rejected as premature
  operational overhead for a single-author v0.1.

## Consequences

- README and `pyproject` positioning/wording change.
- New CI workstream: the dependency-license gate.
- Relaxes [0005](0005-lean-core-experimental-isolation.md)/[0006](0006-quality-bar-invariants-and-benchmark.md):
  a real embedding model is licence-and-lock-in acceptable; it is an extra for weight,
  and the offline default stays the pure placeholder, clearly labeled.
