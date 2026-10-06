---
covers:
  - src/ragspine/extraction/evidence/
verified-against: c7da5dd
---

# extraction/evidence — agent contract

Auto-loaded when working under `src/ragspine/extraction/evidence/`. PDF → source observations,
model-free proofs and typed IR for the traceable PDF evidence chain (the enterprise-pdf-rag
product line, moved here from `enterprise_pdf_rag.{documents, figures, processing}` by
[ADR 0022](../../../../docs/adr/0022-dissolve-enterprise-pdf-rag-into-domain-evidence-subtrees.md);
the legacy names still import, as the same module objects, with a `DeprecationWarning`).
Long-form docs: `docs/enterprise-pdf-rag/` (ADR 0002 / 0008 / 0009 / 0014 / 0015 / 0016,
`testing-and-ingestion.md`). This is **not** the StyledGrid path: `extraction/ir.py` and
`extraction/tables/` are a separate implementation, deliberately not merged yet.

## What lives here

```
document/     pure source model — models.py (spans, regions, assets), ports.py, service.py (source
              identity checked before any SDK call), text_layer.py (per-page text-layer
              diagnosis ok / outlined_text / garbled — detection only)
page/         selected-page processing records and coverage checks — models.py, service.py,
              ports.py, geometry.py (model region vs canonical geometry, 1e-6 tolerance),
              column_regions.py, text_lines.py (lines, the headline, running header / footer
              lines — model-free page geometry, ADR 0025)
metadata/     zero-model derivations — page_metadata.py / periods.py / document_metadata.py
              (verbatim page metadata, deterministic period forms and `period_labels`,
              document fold — ADR 0013), deterministic_metadata.py (a page-metadata candidate
              read off the page's geometry, verified like a model's — ADR 0025),
              document_tree.py (table-of-contents fold: verbatim titles carrying their evidence,
              two cut rules, leaves tiling the document as a type invariant — ADR 0019)
objects/      typed_ir.py (object payloads); tables/ (table_grid_proof.py, the ruled-grid proof —
              ADR 0014; table_transcription.py, the literal transcription rule; table_rows.py, verbatim
              printed rows of a region with no detected grid — ADR 0027; table_inferred_grid.py,
              a model-inferred grid filled from the text layer and kept pending — enterprise-pdf-rag ADR 0031); diagrams/ and
              formulas/ (the model-free proofs and their deterministic projections — ADR 0015)
figures/      pure figure / chart pipeline — same-SVG two branches, snapshot binding,
              source_label_match.py (the ADR 0016 window rule: one to three adjacent source
              occurrences, folded and concatenated, must equal the string being kept);
              chart_qa/ (typed ChartQA: lookup and ordered percentage-point difference only);
              ports.py also declares the optional `BatchEmbeddingPort` (ADR 0026)
```

The I/O side of this domain (pdfspine bindings, source paint, shapes, qualification, model
semantics, stored ChartQA resolvers) still lives in `enterprise_pdf_rag/adapters/` until it moves
to `extraction/evidence/adapters/`.

## Invariants (do not break)

- **Pure** — every package here imports only the standard library, its sibling evidence pure
  packages and `from ragspine import _lazy_submodules`; no `os` / `pathlib` / `io` / network /
  `open()` (`scripts/enterprise_pdf_rag/check_architecture.py`), and the import surface is a closed
  whitelist (`check_conformance.py`). SDKs and I/O belong in adapters, reached through `ports.py`.
  Each `__init__.py` is a docstring (with its `Submodules:` index, half-width punctuation only)
  plus the two `_lazy_submodules` lines.
- **Same-SVG two branches, snapshot binding, no-summary-fallback** — hard invariants of the
  figure chain (ADR 0002). Scoped exception: lite ingest derives a chart's description branch
  from its own IR's printed label fields instead of a second model call (ADR 0025); it binds the
  same SVG and adds nothing the label scope would not re-project from the IR itself.
- **Source review ≠ semantic qualification** — only source-qualified facts reach ChartQA
  (ADR 0008 / 0009); values are never derived. A chart carries exactly one of **two named
  scopes**, and its receipt always says which: `explicit-distribution-shares` (native sector
  geometry plus complete source-paint accounting — ADR 0008) or the default
  `source-labels-and-verbatim-points-v1` (ADR 0016: a point survives only when its category
  **and** its value with unit each print verbatim inside the figure, fail-closed per point,
  never per figure; the category-to-value *association* stays the model's assertion, so the
  two scopes are named apart, stored apart and never merged). `figure-source-labels-only-v1`
  is frozen byte-for-byte so already-published members keep mounting and replaying; every gate
  accepts both, and `chart_publication.resolve_chart_member` re-derives a member under the
  scope its own receipt declares.
- **A verified table grid means ink** (ADR 0014) — `TableIR` / `TableCell` are `VERIFIED` only
  with `GridEvidence` / `CellBorderEvidence`: every row and column boundary sits on a real
  ruling, every cell edge is continuously ruled, every merge is proved by the absence of a rule
  inside it, and the whole proof is re-derived from the pinned source on every resolve. `row` /
  `col` / `header` citations open only for a verified grid, and `header` only names a header
  *proved* by a thick rule or a fill — never a font or first-row heuristic. An unruled, snapped
  or double-ruled table stays `PENDING`, and stays retrievable and citable by cell text.
  A table where nothing is detected at all (unruled / frame-only) is left out unless the
  ingest plan's `unverified_tables_as_rows` (on in lite, off in full) reads it as verbatim printed rows (`tables/table_rows.py`,
  ADR 0027): rows are bbox geometry only, re-derived on every resolve, cited as quotes — never
  cells, columns or headers. With `unverified_table_structure="tsr"` (enterprise-pdf-rag ADR 0031) such a table
  first gets a grid from a structure model (`tables/table_inferred_grid.py`): the model gives only
  slots and spans, every cell text and coordinate comes from the text layer, the `TableIR` stays
  `PENDING` (scope `tsr-inferred-grid-v1`), the model is re-run and the IR compared on every
  resolve, and only exact `cell` claims cite it; a self-check failure falls back to the rows.
- **A retrievable diagram or formula is proved without a model** (ADR 0015) — `DIAGRAM` and
  `FORMULA` objects reach the index only through a pure, replayable proof that runs beside (never
  inside) the two model branches: a node label must equal its cited span verbatim and its bbox must
  match a real painted frame, an edge needs a connector plus a filled arrowhead whose derived tip
  lands in the target, and every span inside the object must be cited; a formula token quotes a
  span substring under a tiling closure rule, a script is proved from the PDF's own `Ts` or marked
  `derived` (`proof_level="literal"`, `PENDING`), and every drawn path inside the object must be
  explained. One failed rule withholds the whole object with a verbatim diagnostic. Their
  `qualified_description` is a deterministic template, never a second model pass, and every resolve
  replays the proof from the pinned source. An `IMAGE` is still not retrievable.
- **One text criterion for names and symbols** — `verify._exact` (whitespace folded, case kept) is
  the single function behind the literal-transcription check, a cited table header (ADR 0014) and
  every diagram / formula claim, so the verify side is never laxer than the qualification side.
  Only cell *content* uses the case-folded `_norm`.
- **pdfspine is the only PDF parser**; PNG wrapping is not structured extraction.
- **A garbled span never enters the sidecar** — a span with any undecodable character (U+FFFD,
  private use, unassigned, surrogate, non-whitespace control) is withheld at extraction, so it
  can never be cited, certified or indexed; each page's `text_layer` keeps the counts and names
  `outlined_text` / `garbled` pages, which `ingest` reports as `ocr_needed_pages`. Nothing is
  OCR'd yet: those pages' words are simply absent from the index.
