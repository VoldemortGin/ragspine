---
status: accepted
date: 2026-09-20
---

# ADR 0021 — Merge enterprise-pdf-rag as a sibling package

> Immutable record. ADRs are exempt from drift tracking. To reverse this
> decision, add a new ADR that supersedes it rather than editing this file.

## Context

Two repositories owned by the same author had grown complementary halves of
one product:

- **enterprise-pdf-rag** (`~/startup/enterprise-pdf-rag`, "A") — a traceable
  financial-PDF backend: content-addressed **immutable snapshots**
  (source → processing → retrieval), an **evidence chain** from PDF
  span/drawing to answer, and **source-qualified ChartQA** (its ADR 0008 /
  0009). Its answer side stops at source review: `POST /v1/chat/completions`
  returns 422 for financial questions and there is no general
  natural-language answer chain.
- **rag-spine** (this repo, "B") — the retrieval / rerank / answer chain
  (`HybridRetriever`, listwise rerank, the anti-fabrication agent
  orchestrator), with no notion of immutable evidence snapshots or
  chart-level qualification.

Keeping them apart meant two toolchains (uv locks, ruff / mypy configs, CI
gates), two copies of the family conventions, and an integration whose first
real step — feeding A's published retrieval snapshot into B's retriever —
would have needed a published package boundary before either side had a
stable seam. Both already pin the same family engine (`pdfspine`) and the same
Python floor.

## Decision

Bring A into this repository as an **independent top-level package**, not as
a `ragspine` subpackage.

- **Layout** — `src/enterprise_pdf_rag/` (import name unchanged),
  `tests/enterprise_pdf_rag/` (own `conftest.py` carrying the offline
  `no_network` guard), `scripts/enterprise_pdf_rag/`,
  `config/enterprise-pdf-rag/`, `data/benchmarks/enterprise-pdf-rag/`,
  `deploy/enterprise-pdf-rag/`, `docs/enterprise-pdf-rag/` (its README,
  handoff, PRD, ADR 0001–0010, schemas). The package contract is
  `src/enterprise_pdf_rag/CLAUDE.md` + `AGENTS.md`, following the
  one-`CLAUDE.md`-per-module convention.
- **One `pyproject.toml`** — both packages are built into the same
  distribution; runtime dependencies are merged and **all upgraded to their
  latest releases** (commit `8ce3301`); `requires-python = ">=3.12"` is
  aligned with `pdfspine`. Two console scripts: `ragspine` and
  `enterprise-pdf-rag`.
- **Lint / type gates scoped by directory** — one ruff config whose global
  `select` is A's strict rule set, with `per-file-ignores` switching off the
  rules the `ragspine` subtree never enabled, so `ragspine` gains no new
  warnings and no mass rewrite (the only formatting change is the separate
  pure-format commit `95f607e`). `mypy` is `strict = true` repo-wide with a
  `ragspine.*` override that keeps ADR 0011's recorded divergences.
- **One pytest run** — `tests/` is collected once; `scripts/ci.sh` step 5
  runs both suites. A's four structural guards (`check_conformance.py`,
  `check_architecture.py`, `check_schema.py`, `check_drift.py`) become
  **step 9**, explicitly scoped to `src/enterprise_pdf_rag` +
  `docs/enterprise-pdf-rag` so they never judge `ragspine`.
- **History preserved** — A was imported with `git subtree` (`0f8499c`) and
  relocated in `407849e`, so `git log --follow` still reaches every original
  commit. The original repo's `merge/into-ragspine` branch is the pre-merge
  snapshot.
- **Runtime scene** — A's gitignored `data/` (published snapshots and
  `current-*` pointers, validation evidence, samples) was **APFS-cloned**
  into this repo's `data/`; the originals stay in the old repo untouched.
  Nothing under `data/` is committed.

## Alternatives considered (rejected)

- **Fold A into `ragspine.enterprise_pdf` now.** A's import name is baked
  into its installed console script, Dockerfile, evidence files and docs;
  renaming before the answer chain is wired would be churn without a design
  reason. Revisit once the integration exists (see follow-ups).
- **Keep two repos and depend on A from PyPI.** Neither side has a stable
  API for the integration seam yet; a package boundary would freeze the
  wrong interface.
- **Run A's tests and gates as a separate CI lane.** A single collection
  surfaces fixture / plugin conflicts immediately and keeps "green" one
  number.

## Consequences

- One repo, one lock, one gate: `bash scripts/ci.sh` is green for both
  packages at `407849e`, and `docs/enterprise-pdf-rag/` was rewritten to the
  new paths.
- `ragspine` behaviour, rule set and type strictness are unchanged; A's
  strict rules apply only to its own tree.

### Follow-ups (not settled by this decision)

- **HTTP client** — `httpx` (base / `[service]`) and `httpx2` (A's tests,
  `ASGITransport` for socket-free ASGI tests) coexist; pick one.
- **`fastapi` / `uvicorn`** are declared both in the base dependencies (A
  needs them unconditionally) and in the `[service]` extra; collapse to one
  place.
- **`deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile`** still runs
  `uv sync --locked` inside the image, where `[tool.uv.sources]`'s local
  `../corespine` path does not exist; it needs `--no-sources` (or an
  equivalent that resolves `corespine` from PyPI). **Not yet verified** — no
  Docker daemon was available.
- **The integration this merge exists for** — connect A's natural-language
  answer chain to B's `HybridRetriever` / rerank / agent orchestration,
  consuming A's `DraftPublication` (source / processing store roots +
  `current_processing_id`) instead of a hard-coded AIA store, while keeping
  A's immutable-snapshot, corrupted-evidence-refusal and
  no-implicit-model-call invariants and B's anti-fabrication / provenance
  invariants intact.
- **Namespace** — decide, after the integration, whether
  `enterprise_pdf_rag` moves under `ragspine.*` or stays a sibling.
