# ADR 0037: A deterministic chart proposer before the chart model call

Status: Accepted, 2026-10-08. Reverses one line of [ADR 0025](0025-lite-ingest-mode.md)'s "Not
done here" ("Deterministic chart IR: the user keeps the chart IR on the model"), **only for the
two grammars below and only when the proposal survives the model IR's own qualification**.
[ADR 0009](0009-source-qualified-expense-ratio-bar-lookup.md) (numbers come from explicit text
spans only) and [ADR 0016](0016-verbatim-chart-points.md) (the verbatim-points scope) are
unchanged. Switch: `APP_CHART_DETERMINISTIC_FIRST` (`Settings.chart_deterministic_first`),
default **on**; `false` restores the previous behaviour byte for byte.

## Context

Every Chart object costs at least one VLM call (`chart-branch-v1`, image + observations), and in
full mode a second one for its description. On the reports this pipeline ingests that is the
largest remaining per-object model cost after layout ([ADR 0030](0030-onnx-layout-partitioner.md))
and page metadata ([ADR 0025](0025-lite-ingest-mode.md)) went local. Many of those charts are
simple: every point prints its own number. For those the model adds nothing an answer can rely
on — qualification ([ADR 0016](0016-verbatim-chart-points.md)) keeps only what the figure prints
verbatim anyway — but it does add latency, budget and a non-deterministic association step. The
user decided to let deterministic code propose the IR for such charts.

The repository already has geometry that settles *which printed label belongs to which mark*
without reading any value from the mark: `bar_geometry.match_direct_bar_labels` (upright bars,
one period category below and one percent label above each) and
`donut_geometry.native_donut_geometry` plus the label relations of `donut_qualification`
(two-sector annulus, percent label inside a sector, category beside it on a clear corridor). They
were written as after-the-fact verifiers (ADR 0008 / 0009). This ADR runs them forward, as a
proposer.

## Decision

`adapters/deterministic_chart_proposer.py`, called by `SemanticObjectAdapter._chart` after the
view is prepared and before `ModelChartExtractor`:

1. **Input** is what the pipeline already holds: the `PreparedFigure` (cropped native SVG, the
   fully contained source text observations, `paint_text_spans`). No new pdfspine API. Only a
   donut whose glyph outlines need explaining reads the source PDF, through the existing
   `build_source_paint_proof` replay, and only when the crop holds exactly two coloured fills.
2. **Two grammars**, tried in order:
   - *Vertical bar* (`direct-percent-bar-labels-v1`): every coloured fill in the crop must be an
     upright rectangle (glyph outlines inside a printed span's box, a background under the whole
     crop and unfilled strokes are passed over; a fill touching any printed span — a label inside
     a bar, a stacked segment — refuses). `match_direct_bar_labels` assigns one category below and
     one percent label above each bar, at least two bars, one baseline, no overlapping columns.
     Every bar must carry its label; the one remaining span above the plot is the title, which is
     also every point's series (as `bar_qualification` does). Anything else unexplained refuses.
   - *Two-sector donut* (`two-sector-donut-direct-labels-v1`): `native_donut_geometry` proves the
     annulus; each percent label lies inside exactly one sector and has exactly one category on a
     clear corridor beside it, one-to-one; the centre may print one metric (the series) and one
     `1H26`-style period; at most one more span, above the ring, is the title (the series when no
     metric is printed). Anything else refuses.
3. **Values** are `ValueKind.EXPLICIT` decimals read from the number substring of the printed
   percent label (`prepare_figure`'s `15%` → `15` + `%` fragments), cited element by element.
   Geometry never supplies a number: not a bar height, an arc length, a colour or an axis.
4. **Admission** (`admit_proposal`): every point's value must be explicit and printed by its own
   cited source text (`match_source_value`); then the proposal and its label description
   (`describe_from_ir`, the ADR 0025 projection) go through **the very qualification the model IR
   faces** for the adapter's policy — `qualify_source_labels` for `none` (which
   `visual_requalification` applies later) and `source-labels-only`, `DonutQualification` for
   `donut` — and every proposed point must survive it. Nothing here is looser than the model path.
5. **Fallback**: any refusal (grammar not recognised, a label missing or ambiguous, zero points,
   qualification failing) raises `ProposalRejected(code)`; `_chart` writes nothing and continues
   down the unchanged model path. The record a fallback writes is byte-identical to the one this
   code wrote before (pinned by test).
6. **Provenance**: `ChartIR.producer = deterministic-chart-proposer/v1:<rule>`; the qualified
   projection keeps it under its own prefix (`verbatim-source-points-v1:deterministic-…`). Stages
   of an admitted proposal (`ir`, `ir_diagnostics`, `description`, then qualification) are saved
   under the object producer suffixed `:deterministic-chart-proposer/v1`, so they never share a
   stage fingerprint with a model run of the same object; no `ir_raw` / `description_raw` exists
   because no model answered. `execution_mode` is `PRODUCTION`: the enum's only offline value is
   `offline-demo`, which every qualification refuses and which would mislabel a real ingest; the
   producer is what distinguishes the two sources.
7. **Model cache** is untouched: an admitted proposal sends no request, so no model-cache record,
   response or context is written; a fallback sends exactly the requests it sent before.
8. **Trace**: `SemanticObjectAdapter.chart_proposals` counts `proposed` / `fallback`; one debug
   log line per chart carries the outcome, the rule or refusal code, the point count and the
   elapsed milliseconds. Codes and counts only, never label text.

## Consequences

- A proposed chart's qualified IR, qualified description, receipt and index text are the ones the
  model path produces for an equivalent model answer (pinned by test), except for the producer
  strings. `resolve_chart_member` re-derives it from the stored IR and description like any other
  member (pinned by a folder-run test on an authored PDF).
- In full mode a proposed chart's description is the label projection of its IR (as in lite),
  not a model description: a label only the model description named (a footnote, say) is no longer
  indexed for that chart. Its numbers were never taken from the description.
- The geometry was written for the AIA layouts and is reused as is: bar categories must be
  half-year periods (`[12]H\d\d`) and bar / sector labels plain percentages. A chart printing
  years, `$m` values, wrapped labels, a legend or any extra text falls back to the model.

## Not done here, and why

- **Line, waterfall, stacked, grouped and dual-axis charts, pies**: always fall back. Grouped bars
  share one category per group, which `match_direct_bar_labels` (one category per bar) refuses;
  a pie is not an annulus.
- **Widening the label grammar** (years, currency / unit labels, labels inside bars): would mean
  generalising `bar_geometry` / `donut_geometry`, which are also the ADR 0008 / 0009 verifiers;
  left for a separate decision.
- **No value is ever derived** from geometry, and no new qualification scope is introduced.
- **No summary field**: `IngestionSummary` is unchanged (adding a field would change its bytes for
  the switch-off run); the counter lives on the adapter.
