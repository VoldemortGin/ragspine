---
status: accepted
date: 2026-06-18
---

# ADR 0011 — Adopt the python-project-standard; migrate to `src/` layout, keep the rest

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Relates to [0009](0009-dependency-and-framework-policy.md) (framework/dependency policy)
and [0005](0005-lean-core-experimental-isolation.md) (lean core).

## Context

An external house standard ("python-project-standard": mypy `--strict` + beartype claw
hook + pydantic-at-boundaries + `ports/`/`adapters/` Protocol seams + `src/<pkg>/` layout
+ a `core/` infra package + a one-command zero-warning gate) was run against this repo.

RAGSpine already satisfies the standard's **intent and most of its substance** by its own
documented conventions: `mypy strict = true`; the beartype claw hook at the top of
`src/ragspine/__init__.py` (guarded import, PEP 484 numeric tower) with the import-ordering
discipline honored; every external dependency behind a `Protocol` with a zero-SDK offline
core and a default `MockProvider`; a domain-first deep layout; TDD; drift guards
(`scripts/check_doc_drift.py`, `scripts/check_docstring_refs.py`); a single CI gate
(`scripts/ci.sh`); and anti-fabrication / provenance enforced in code.

The remaining gaps were **purely structural-layout choices**. Of those, the **top-level
package vs `src/<pkg>/`** is the one the standard treats as a hard, mechanically-checked
invariant, and the one whose migration cost is *bounded*: every in-tree import already uses
the absolute `from ragspine.…` form (zero `from src.…`), so moving the package changes no
application code — only packaging config, one `__file__`-relative root anchor, the tool
path args, and path strings in docs. The other divergences (rootutils, `common/` vs
`core/`, Protocol-via-lazy-import vs `ports/`+`adapters/`, sparse pydantic) carry real
cross-cutting cost for no behavioral gain.

## Decision

Adopt python-project-standard as the governing philosophy, and **align the one bounded,
high-value structural invariant: migrate to the `src/ragspine/` layout.** Concretely:

- `git mv ragspine src/ragspine`; `import ragspine` keeps working via the editable install
  (`.pth` now points at `src/`). No application code changes.
- Config follow-through: hatchling `packages`, mypy `files`, and `[tool.ruff] src` →
  `src/ragspine`/`src`; `common/core.py`'s `Path(__file__)…parents[N]` anchor +1 level;
  `ci.sh`/`Makefile` ruff path args; `check_docstring_refs.py` re-pointed at the new root
  (its old "any `src/…` ref is dead pre-reorg debris" rule is inverted — `src/ragspine/…`
  is now the canonical path); doc + `covers:` path strings re-prefixed `src/`.

**Keep these divergences** (cost ≫ benefit — accepted, not debt):

1. `rootutils` + `.project-root` for root resolution (one site, `common/core.py`,
   import-time zero-side-effect) instead of editable-install + `importlib.resources`.
2. `common/` plays the cross-cutting-infra role the standard assigns to `core/`.
3. SDK isolation via `Protocol` + lazy import (per [0009]/[0005]), not a `ports/` +
   `adapters/` directory split.
4. pydantic at the boundary only where already used, not blanket.

`check_conformance.py` is **not** wired into `ci.sh`: it still asserts a `core/` infra
package, a leaf `core/settings.py`, and `ports/`/`adapters/` dirs this repo deliberately
does not have. `scripts/ci.sh` remains the gate.

## Alternatives considered (rejected)

- **Keep the top-level layout too** (this ADR's original position): rejected — the `src/`
  move is bounded (no app-code churn) and aligns the standard's one hard structural
  invariant, so the minimal-diff argument that protects (1)–(4) does not extend to it.
- **Full literal conformance** (also add `core/`, split `ports/`+`adapters/`, pydantic
  everywhere): rejected — high cross-cutting cost on a mature 1078-test codebase, zero
  behavioral/safety gain.
- **Wire `check_conformance.py` into the gate**: rejected — it would fail permanently on the
  accepted divergences (1)–(4), turning a green gate red for non-issues.

## Consequences

- Package now lives at `src/ragspine/`; the editable `.pth` points at `src/`; every doc and
  `covers:` path string is `src/ragspine/…`. ADRs 0001–0010 are immutable and keep their
  original (now-historical) path references.
- `check_docstring_refs.py` semantics flipped: `src/ragspine/…` is canonical; a bare
  `ragspine/…` or an old flat `src/<mod>.py` is dead.
- New greenfield sibling projects still follow the standard literally (`core/`,
  `ports/`+`adapters/` from day one); this repo retrofits only the `src/` move.
- A future low-risk PR may add pydantic at the `service/` boundary; (1)–(4) otherwise stand.
