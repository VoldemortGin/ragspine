---
status: proposed
date: 2026-09-23
---

# ADR 0022 — Dissolve enterprise_pdf_rag into per-domain `evidence/` subtrees

> Immutable record once accepted. ADRs are exempt from drift tracking. To
> reverse this decision, add a new ADR that supersedes it rather than editing
> this file. Status stays **proposed** until the last migration batch lands.

## Context

ADR 0021 brought enterprise-pdf-rag in as an independent top-level package
(`src/enterprise_pdf_rag/{core, documents, figures, processing, answers,
adapters}`) and deferred the namespace question. The package is organized by
technical layer — one flat `adapters/` holds 78 modules spanning PDF parsing,
qualification, ingestion stages, storage, retrieval, answering, evaluation and
HTTP — which is exactly the shape this repo's *package-by-feature* convention
rejects: the folder no longer locates the file. Meanwhile the same
responsibilities already have homes in `ragspine` (`extraction`, `ingestion`,
`storage`, `retrieval`, `agent`, `eval`, `service`, `cli`).

## Decision

Split the package by responsibility into one **feature subtree named
`evidence/` inside each `ragspine` domain**, and keep the old import paths
alive through a runtime shim.

- **Layout** — `ragspine.<domain>.evidence` for `common`, `extraction`,
  `ingestion`, `storage`, `retrieval`, `agent`, `eval`, `service`, plus
  `ragspine.cli.evidence` for the console script. Files keep their names; the
  one rename is `processing/retrieval.py` → `retrieval/evidence/index/snapshot.py`.
  One subtree name per domain keeps the duplicated capabilities side by side
  and grep-able (`extraction/evidence/objects/tables` vs `extraction/tables`)
  instead of interleaving them, and needs one rule rather than a per-file
  debate. This is a **move, not a fusion**; the overlaps are listed below.
- **Pure / impure boundary unchanged** — the old pure packages (`documents`,
  `figures`, `processing`, `answers`) land only in pure directories
  (`extraction/evidence/{document, page, metadata, objects, figures}`,
  `retrieval/evidence/index`, `agent/evidence/{answers, context}`); modules
  from `adapters/` land only in I/O directories (`…/evidence/adapters/` in
  domains that hold both, the subtree itself in I/O-only domains).
- **Frozen map** — `src/enterprise_pdf_rag/_moves.py` (`MOVES`: 139 legacy →
  canonical module names; `PACKAGES`: the 8 legacy subpackages; `PENDING`: the
  AIA lane below) is generated once and frozen. It drives the shim, its tests
  and the import codemod `scripts/enterprise_pdf_rag/rewrite_legacy_imports.py`.
- **Shim** — `enterprise_pdf_rag._shim.LegacyFinder`, first on
  `sys.meta_path`, binds a legacy name to the *same* module object as its
  canonical name (so class identity, `isinstance`, pickling and `monkeypatch`
  by dotted string keep working), emits a `DeprecationWarning` attributed to
  the importing line, and turns emptied legacy packages into virtual packages
  that expose their moved children lazily. Until a module moves, the finder
  steps aside. `python -m enterprise_pdf_rag.cli` keeps working.
- **One repo, one `pyproject.toml`, one gate** — unchanged from ADR 0021.
  The strict ruff set and mypy's two extra checks (`warn_unreachable`,
  `ignore-without-code`) follow the code: they are scoped to `**/evidence/**`,
  `ragspine.cli.evidence` and the shim by a negated ruff glob and a
  `ragspine.*.evidence.*` mypy override, both verified by probe files, so a
  file moving into `src/ragspine/**` never silently loses them.
- **Unchanged names (D6)** — the `enterprise-pdf-rag` console script, the
  wire formats (the `enterprise_pdf_rag` JSON key, `enterprise-pdf-rag/<sha12>`
  model ids), `config/`, `data/`, `deploy/` and `docs/enterprise-pdf-rag/`
  paths, and `scripts/enterprise_pdf_rag/` (D7).
- **AIA sample lane leaves the wheel (D3)** — the AIA-specific modules
  (`adapters/aia_ingestion`, `aia_processing`, `aia_candidates`,
  `source_review_html`, `documents/aia`) and
  `resources/aia-first-20-regions.json` do **not** move into `ragspine`: a
  named company and real-document regions conflict with the
  config-driven / no-real-data invariants. They are `PENDING` in the map and
  stay at their legacy paths until the ingestion batch decides their landing
  place outside the wheel. That batch first has to separate the generic
  helpers these modules currently host (`read_text_sidecar`,
  `stage_fingerprint`, `ProcessingPipeline`, `export_review`, used by ~13
  generic modules) from the AIA-specific lane.
- **Schemas (D2)** — pydantic `$defs` keys in the published JSON schemas
  carry module-qualified names and change with the move; the regenerated
  schemas are accepted with a normalized diff proving only `$defs` names
  changed. Payloads on the wire are unchanged.
- **Runtime type checking (D4)** — the moved code is instrumented by
  `ragspine`'s unconditional beartype hook (with the PEP 484 numeric tower);
  `APP_BEARTYPE_ON` stops having an effect. No deployment sets it.
- **Deprecation window (D5)** — the shim ships in 0.17.0 and stays for at
  least two minor releases; removing it needs its own ADR.

## Alternatives considered (rejected)

- **Keep the sibling package (status quo of ADR 0021).** Leaves a
  layer-organized tree beside a feature-organized one and keeps the overlaps
  invisible.
- **Place modules directly in existing domain folders, no `evidence/` layer.**
  Collides with existing names (`extraction/ir.py`, `extraction/tables/`,
  `retrieval/lexical/retrieval.py`, `service/api/`) and mixes two
  implementations of the same capability in one folder.
- **Fold into a single `ragspine.enterprise_pdf` subpackage** (ADR 0021's
  rejected option). Moves the name without fixing the layer-shaped layout.
- **Move the AIA lane into `ragspine` as a recorded exception.** Rejected by
  the owner: company-specific sample code and region data do not ship in the
  engine's wheel.

## Consequences

- Supersedes ADR 0021's *Layout* and *lint / type gates scoped by directory*
  parts; ADR 0021 itself is not edited.
- Compatibility holds **at runtime only**: mypy / pyright cannot resolve the
  legacy paths, so typed downstream code must switch to the canonical names
  (the codemod does it).
- Old `importlib.resources` paths under `enterprise_pdf_rag/resources/` get no
  compatibility; only internal code used them.
- Cross-domain "upward" dependencies become visible (qualification →
  ingestion's sidecar reader, ingestion → service schemas, retrieval → agent
  answers, storage → retrieval index text); they are listed for later, not
  fixed by the move.

### Fusion candidates (not settled by this decision)

Two pdfspine lanes (evidence observations vs `StyledGrid`) and two text-layer
triages; grid proof vs the TSR seam and two IRs; `hybrid_search` vs
`HybridRetriever`; `query_mode` / `query_filters` / `index_text` /
`tree_retrieval` / `page_window` vs `retrieval/{mode, filtering, contextual,
raptor}` and parent-child expansion; `verify` + `answer_service` vs the
agent's anti-fabrication guard (the integration ADR 0021 left open); model
access; APP_* settings vs `ServiceConfig` / `RAGSpineConfig`; two FastAPI apps,
two OpenAI-compatible endpoints and two CLIs; `nl_gold` in the QA ratchet;
RESTRICTED isolation, which does not yet cover evidence retrieval; two drift
checkers and two conformance checkers; `httpx` vs `httpx2`.

### Same name, different thing

`chart_qa` exists four times (`extraction/evidence/figures/chart_qa`,
`extraction/evidence/adapters/chart_qa`, `service/evidence/api/chart_qa.py`,
`eval/evidence/chart_qa`); `models.py` in `document`, `page`, `figures` and
`answers`; `extraction/evidence/objects/tables` is not `extraction/tables`.

## Appendix — module map

The authoritative map is `src/enterprise_pdf_rag/_moves.py`. By destination:

| Canonical package | Legacy modules |
|---|---|
| `common.evidence` | `core.{settings, logging}` |
| `common.evidence.providers` | `adapters.{providers, json_completion, local_models, local_model_launcher, local_model_tunnel}` |
| `extraction.evidence.document` | `documents.{models, ports, service, text_layer}` |
| `extraction.evidence.page` | `processing.{models, service, ports, geometry, column_regions}` |
| `extraction.evidence.metadata` | `processing.{page_metadata, periods, document_metadata, document_tree}` |
| `extraction.evidence.objects` (+ `tables`, `diagrams`, `formulas`) | `processing.{typed_ir, table_*, diagram_*, formula_*}` |
| `extraction.evidence.figures` (+ `chart_qa`) | `figures.*`, `figures.chart_qa.*` |
| `extraction.evidence.adapters.{pdfspine, source_paint, shapes, qualification, semantics, chart_qa}` | the PDF, source-proof, geometry, qualification, model-semantics and stored-ChartQA adapters |
| `storage.evidence` | `adapters.{document_store, processing_store}` |
| `retrieval.evidence.index` | `processing.{retrieval → snapshot, index_text}` |
| `retrieval.evidence.adapters` | `adapters.{hybrid_search, tree_retrieval, processing_retrieval, document_catalog}` |
| `ingestion.evidence.{pipeline, stages, publication}` | the ingestion entry, stage and publication adapters |
| `agent.evidence.answers` / `.context` / `.adapters` | `answers.*` / `processing.context_builder` / `adapters.{answer_service, answer_audit, query_translation}` |
| `eval.evidence` (+ `chart_qa`) | `adapters.{nl_gold, retrieval_evaluations}`, `adapters.chart_qa_*{evaluation, capture, targets}` |
| `service.evidence.api` / `.demo` | `adapters.http.*` / `adapters.{runtime, memory, offline, demo_source, qualification, review}` |
| `cli.evidence` | `cli` |
| *pending (outside the wheel)* | `adapters.{aia_ingestion, aia_processing, aia_candidates, source_review_html}`, `documents.aia`, `resources/aia-first-20-regions.json` |
