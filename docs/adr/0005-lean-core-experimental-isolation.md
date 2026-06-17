---
status: accepted
date: 2026-06-17
---

# ADR 0005 — Lean core; quarantine dormant capability as experimental/extras

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Gated by [0003](0003-audience-oss-library.md).

## Context

Nearly every subsystem carried code that is built and tested but **not wired into the
running pipeline**: the OCR scanned-PDF extractor (parked in the review queue, never
invoked), the dual-channel verifier (orphaned, only its own test calls it), the vector
channel (fully wired but the only offline backend is a self-described non-semantic
lexical hash; real embeddings deferred to GPU infra), color-legend detection
(test-only), an OpenAIProvider stub, and two parallel extraction families (legacy
`extract_facts` vs `StyledGrid`). For a library people evaluate, dead-looking code that
doesn't actually run is a trust problem.

## Decision

Ship a **lean core** containing only what runs in the default offline path: BM25 +
structured channel + agent + anti-fabrication. Move OCR, real semantic vector, and the
dual-channel verifier into **clearly-labeled experimental modules / extras**,
documented as "wired, needs GPU/extra, not covered by default guarantees." Keep **one
live extraction path** (retire or quarantine the legacy/`StyledGrid` duplication). Each
experimental module is promoted into the core only once it has a real, CI-tested path.

## Alternatives considered (rejected)

- **Wire everything in before any 1.0 (option A)**: give vector a real offline CPU
  model, add an OCR CI lane, connect the verifier. Rejected as too heavy for now (it is
  the eventual direction for individual modules as they earn promotion).
- **Keep as-is, document honestly (option C)**: leave the "ship contract, defer
  activation" posture and just list gaps. Rejected — reads as "impressive-looking code
  that doesn't run."

## Consequences

- The default `pip install` experience is honest and runnable end-to-end, at the cost
  of being non-semantic by default (consistent with the lean-default of [0009](0009-dependency-and-framework-policy.md)).
- README "honest gaps" must clearly mark every experimental module.
- Defines the promotion criterion: a module enters core when it has a real CI-tested
  path and (for retrieval) backs the benchmark in [0006](0006-quality-bar-invariants-and-benchmark.md).
