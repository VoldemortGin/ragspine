# ADR 0025: A lite ingest mode that sends only the calls a fact is read from

Status: Accepted, 2026-10-05. A scoped revision of [ADR 0002](0002-figure-pipeline.md) /
[ADR 0005](0005-first-twenty-pages-processing.md) (same-SVG two independent branches) for the
chart description, and of [ADR 0013](0013-page-metadata-and-prefilters.md) (page metadata is
extracted by a model) — **in lite mode only**. `run_folder_pipeline(ingest_mode="full")`, the
default, is unchanged byte for byte. (Not to be confused with the repository's main ADR 0025 in
`docs/adr/`, a separate number space.)

## Context

The pipeline was designed for slide decks printed to PDF. It is now asked to ingest a folder of
encrypted insurance-company reports of several hundred pages each — mostly prose and tables, a
few charts — and answer a few hundred factual questions on Databricks, where the LLM (Azure
OpenAI) is billed per call and takes seconds, and the project root sits on Workspace files
(FUSE: every file operation is a network round trip; a Git folder should stay under ~20 000
files). Calls and files are both cost.

An ingest sent, per document, about P (layout, with image) + P (page metadata, text only) + 2C
(chart IR + description) + 2V (two branches per image / diagram / formula) + T (tree routing
notes) calls. Reading each call's consumers:

| call | who reads its output | can an answer depend on it? |
| --- | --- | --- |
| layout `page-layout-v2` | every object | yes |
| chart IR `chart-branch-v1` | `qualified_ir` — the only source of a chart's numbers (ADR 0016) | yes |
| chart description `description-branch-v1` | `qualify_source_labels`: every numeric claim is dropped (`numeric_claim_not_a_label`); the label claims it keeps are re-projected from the IR's own printed fields anyway | no (labels only, also read from the IR) |
| diagram IR + description | the ADR 0015 proof needs both | yes |
| image IR + description | nothing: `eligibility()` refuses every Image | no |
| formula IR + description | lineage only: the ADR 0015 proof reads the PDF; a budget-exhausted formula already qualifies | no |
| page metadata `page-metadata-v1` | index-text header, period / region pre-filter, cover / agenda exclusion, `display_title`, tree | indirectly (ranking); every filter relaxes when it starves |
| tree `document-tree-summary-v1` | a third ranking (ADR 0019: changed no verdict on the sample) | no |

## Decision

`run_folder_pipeline(..., ingest_mode: Literal["full", "lite"] = "full")` (CLI
`run-folder --ingest-mode`, notebook `INGEST_MODE`, default `"lite"` there). The mode only picks a
preset of `adapters/ingest_mode.IngestPlan`, whose every field is one switch; `"full"` is the
all-default plan.

| switch | full | lite | what replaces the call, and the guarantee it keeps |
| --- | --- | --- | --- |
| `image_semantics` | 2 calls | 0 | The Image is still registered (crop + source text, so every span keeps its owner and `validate_partition` holds); `ir` / `description` / `qualification` are `not_applicable` with diagnostic `skipped_by_ingest_mode: …`. `eligibility()` gives the same verdict as before. No change in what can be retrieved. |
| `formula_semantics` | 2 calls | 0 | The view is still prepared (`svg` / `model_render` / `model_view`), both branches are `not_applicable`; `_formula` then runs exactly the path a budget-exhausted formula already took: the proof reads the pinned PDF, `lineage` is only the model view, `check_model_description` records `formula_model_description_unavailable`. The qualified IR / description / observation are the **same bytes** as in full (pinned by test). |
| `chart_description` | 1 call | 0 (`from-ir`) | `chart_semantics.describe_from_ir`: one label claim per printed prose field of the IR (title, period, axis label / unit, point category / series — `label_fields` order), no numeric part, producer `chart-description-from-ir-v1:<chart producer>`, same binding, `PENDING`. The chart IR call is the identical request, so `ir` and the requalified `qualified_ir` are the **same bytes** as in full (pinned by test). |
| `page_metadata` | 1 call / page | 0 (`deterministic`) | `metadata.deterministic_metadata.deterministic_candidate` over `page.text_lines`: title = the largest line in the top half of the page (never a running line); section = the first running line in the top half (same text at the same 5 pt height band on ≥ 30 % of the selected pages and on ≥ 2 pages); periods = every label `periods.period_labels` finds, cited to its span; regions = none; page type `other`; language none. The candidate goes through `verify_page_metadata` unchanged (a value not verbatim in its span window is dropped, never repaired). Producer `page-metadata-deterministic-v1`; the stage fingerprint binds the page's canonical artifact and the document's running-line set; the record is saved but not stage-cached (recomputing is cheaper than a file). |
| `build_tree` (default) | yes | no | `build_tree=None` follows the mode; an explicit `True` in lite builds the tree from the deterministic metadata (tested). |
| `review_exports` | yes | no | `export_review` / both `export_processing_review` calls are skipped; `IngestionSummary.review_path` / `DraftIndex.review_path` are `None`. `pdf_ingestion.export_document_review(<ingestion root>/<sha256>)` writes the same pages on demand (no model). |
| `layout` | `model` | `model` | The partitioner selection point (`ingest_mode.make_partitioner`); the deterministic text-page layout of the next step plugs in here. |

Skipped calls never reach the client, so they spend no budget; `"auto"` (pages × 4 + 50) is simply
a looser ceiling in lite.

### Coexistence of the two modes on one ingestion root

- Source stage, layout cache and model cache are shared; lite object stages carry the producer
  suffix `:lite-v1` and lite page metadata its own producer, so the two modes' processing
  snapshots have different ids and never overwrite each other's assets.
- lite → full on the same root sends **only** what lite skipped (page metadata, chart
  descriptions, image / formula branches); layout, chart IR and diagram calls replay. The full
  snapshot published then is byte-identical to a fresh full run (same processing id; tested).
- full → lite sends **zero** live calls.
- There is one `current-processing` pointer: the last successful publication wins.
  `DocumentRun.ingest_mode` is the mode of the run; `DocumentRun.published_ingest_mode` is the
  mode of the snapshot the pointer names after the run, read back from its page-metadata
  producers (`ingest_mode.published_ingest_mode`), so a failed lite rerun reports the full release
  still published.
- An existing `data/ingestion` needs no migration in either direction.

### Visibility

`FolderPipelineResult.ingest_mode`, `DocumentRun.ingest_mode` / `published_ingest_mode`,
`IngestionSummary.ingest_mode` / `skipped_calls` (`{image, formula, chart_description,
page_metadata}` → count), a `report.md` mode line, `mode` / `published mode` table columns and a
per-PDF skipped-calls list, `ingest_mode` on the `discovered` / `document_start` events, and the
notebook status table's `mode` / `skipped_calls` columns. Counts and codes only.

## Revised guarantees (lite only)

- **ADR 0002 / 0005, same-SVG two independent branches.** In lite the description branch is no
  longer independent of the IR: it is a projection of the IR's own label fields. What the
  independence bought in the generic pipeline was never a numeric cross-check — the label scope
  (ADR 0016) drops every numeric description claim and re-projects the IR's label fields itself —
  so the qualified projection, the index text and the citable values are unchanged; a label the
  model description named that the IR did not (a footnote, say) is no longer indexed. The donut
  / bar geometry policies (`explicit-distribution-shares`, ADR 0008 / 0009), which do read numeric
  description claims, are not reachable from `run_folder_pipeline` and are not offered in lite.
  No check in the code requires the description to come from a model; `resolve_chart_member`
  re-derives a lite member from its stored IR and derived description like any other.
- **ADR 0013, page metadata.** Values stay verbatim and verified, but are chosen by geometry, not
  by reading: a headline that is not the page's real title, or a section line that is a running
  footer, can be chosen. Consequences: cover / agenda pages are no longer recognised, so they are
  no longer excluded from the candidates (they compete in the ranking like any page); there is no
  region vocabulary, so a region pre-filter never applies; the period pre-filter keeps working
  from every printed period label (more labels than a model would list — a bare year in a title
  counts) and still relaxes when it starves; `display_title` is the first selected page's
  headline.
- Anti-fabrication, provenance, RESTRICTED isolation, privacy traces, core-imports-no-SDK and the
  offline default are untouched: nothing new is generated, every derived value is printed text
  with its span.

## Not done here, and why

- **Deterministic layout** (no `page-layout-v2` for a page without graphics): the next step; this
  ADR only leaves the selection point (`IngestPlan.layout`, `make_partitioner`) and the reusable
  geometry (`page/text_lines.py`: `text_lines`, `headline`, `running_lines`).
- **Deterministic chart IR**: the user keeps the chart IR on the model — it is the only source of
  a chart's numbers. (Since reversed for fully labelled simple bar / donut charts by [ADR
  0037](0037-deterministic-chart-proposer.md).)
- **Unruled tables as verbatim rows** and **batched embeddings**: separate changes on their own
  branches; lite will switch the former on when it lands.

## Measured (offline, synthetic seven-page report: prose, ruled table, unruled table, bar chart, image, two formulas, diagram)

| | full | lite |
| --- | --- | --- |
| model calls | 24 (layout 7, page metadata 7, chart 2, image 2, formula 4, diagram 2) | 10 (layout 7, chart IR 1, diagram 2) |
| files under the ingestion root | 843 | 406 (−52 %) |
| file opens for write / read | 1387 / 1804 | 822 / 1312 |
| wall time (stubbed model) | 1.41 s | 1.02 s |

Extrapolated (not measured) to a 300-page report with 30 charts and 10 other visual objects
(say 6 images, 2 formulas, 2 diagrams) and a tree of ~20 branches: full ≈ 300 + 300 + 60 + 20 + 20
= 700 calls; lite ≈ 300 + 30 + 4 = 334 calls (−52 %), each saved call also saving its three
model-cache files.
