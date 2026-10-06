# ADR 0031: A table with no ruled grid may get a model-inferred grid, kept PENDING

Status: Accepted, 2026-10-06. Off in both ingest presets; the run-folder notebook's
`UNVERIFIED_TABLE_STRUCTURE = "auto"` turns it on in lite only when the SLANet-plus weights
and onnxruntime are present (see Integration).
Builds on [ADR 0014](0014-ruled-table-grid-proof.md) and
[ADR 0027](0027-unverified-tables-as-verbatim-rows.md); changes neither.

## Context

A long financial report's statements are mostly unruled. ADR 0014 proves a grid only from ink,
so `find_tables(strategy="lines")` finds nothing there, and ADR 0027 then indexes the region as
its verbatim printed rows (`fragments.row-N`, cited as quotes, `structure=unverified`). That
answers "the value on this line", but the association *value → column header* stays the answer
model's reading of a tab-joined line: for a statement with several period columns the model has
to count positions in `Revenue\t1,234,567\t1,100,200` against a header line printed elsewhere.

A table-structure-recognition (TSR) model can propose the rows and columns. RAGFlow does this
with a TSR ONNX model on the table crop, then fills the cells from text boxes. pdfspine 0.11.0
already packages SLANet-plus (PaddleOCR, ONNX export by RapidAI) for its own ONNX backend, and
ragspine has a `TableStructureRecognizer` seam (`extraction/tables/structure.py`) whose contract
is the same split: the model gives coordinates, the text comes from the text layer.

The question this ADR answers is what such a grid may *be* in the evidence chain. ADR 0014 is
explicit: an inferred structure has no ink to cite and must not be presented as table evidence.

## Decision

1. **One more field on the ingest plan.** `IngestPlan.unverified_table_structure:
   Literal["rows", "tsr"]` (`adapters/ingest_mode.py`), `"rows"` in both presets, overridable by
   `ingest_pdf(..., unverified_table_structure=)` and `run_folder_pipeline(...)` like the other
   switches. `"rows"` changes nothing: every stage, fingerprint and processing id is
   byte-identical (pinned). `"tsr"` applies only where ADR 0027's branch fires — `table_detection`
   found no exact region match — and indexes such a table even when
   `unverified_tables_as_rows` is off, because asking for TSR is asking for the table: a grid
   that fails its self-check falls back to the rows. A detected grid (verified or pending) and a
   fully ruled table keep their path whatever the switch says (pinned).

2. **The model gives the discrete structure; the text layer gives everything else.**
   `adapters/pdfspine_tsr.SlanetPlusRecognizer` implements the `TableStructureRecognizer`
   contract: the region is rendered at 144 dpi (pdfspine's whole-page display-list render,
   cropped at whole pixels, 10 px white margin), SLANet-plus returns cells with row / column
   slots and spans in crop pixels, mapped back to page points. Its OCR is never read.
   `extraction/evidence/objects/tables/table_inferred_grid.inferred_table` (pure, stdlib) then
   assigns each of the region's own spans to the one cell whose model box holds its centre, and
   builds an ordinary **`PENDING` `TableIR`** (`grid_evidence=None`): a cell's text is its spans'
   own text in reading order, a cell's box is its column's and its row's printed extent (from the
   spans, not the model), and an empty slot is a `BLANK` cell. Because no model coordinate is
   stored, float noise in the model's boxes cannot change the IR; only a span whose centre moves
   across a cell boundary can.

3. **Self-check, fail closed with a reason code.** Any of these returns an
   `InferredGridRejection` and the table falls back to ADR 0027's rows:
   `no_structure` (the model returned nothing usable — pdfspine's own strict cell check reported
   a problem, or no cell), `grid_too_small` (< 2 rows or columns), `grid_invalid` (a slot
   outside the grid or covered twice), `grid_incomplete` (an uncovered slot),
   `span_unassigned` (a span no cell box holds), `span_ambiguous` (a span two cells hold equally:
   same overlap and same centre distance — overlapping neighbours are the model's normal output,
   so a centre inside two boxes goes to the box covering more of the span, then to the nearer
   box), `empty_row` / `empty_column` (a row or column with no span in a cell of its own, not
   only under a spanning cell), `order_inconsistent` (a cell left of / above another in the grid
   prints a span right of / below it), `no_header_row` (the first row already prints an amount),
   `no_data_row` (no row prints an amount), `model_error` (the runtime raised on this crop) and
   `transcription` (ADR 0011's literal transcription rule refused the grid). The fallback is
   recorded on a `table_structure` stage (`UNAVAILABLE`, diagnostic `tsr_fallback:<code>: …`).
   *Header rows* are the leading rows printing no amount outside the stub column (an amount: sign,
   accounting brackets, currency symbol, separators, decimals, percent; a bare year is a period
   label). They are inferred, used only for rendering, and never cited.

4. **Representation and receipt.** The `ir` / `description` / `qualification` stages carry the
   producer `<semantic writer producer>:table-structure-tsr-v1:pdfspine/<version>:<sha256 of the
   weights, first 12>`; `native_crop`, `source_text`, `svg` and `table_detection` keep their
   bytes. The description is ADR 0011's exact cell transcription (`exact-source-transcription-v1`),
   the receipt a `LiteralQualification` with `scope="tsr-inferred-grid-v1"`, no `grid_scope`, no
   `ruling_digest`. The IR's first diagnostic records the structure producer
   (`structure_producer=…`), so the receipt type and every existing snapshot are unchanged and no
   public schema moves. `eligibility()` is unchanged.

5. **Re-verified on every index and resolve, by re-running the model.**
   `validate_literal_member` runs the full literal-table checks (anchor, crop, span set,
   `check_table_transcription`, a pending grid with no grid fields), then for this scope requires
   the IR to be pending, to record a structure producer, and that the configured model has the
   same producer; it re-opens the pinned PDF, re-runs SLANet-plus on the same region and requires
   the re-derived `TableIR` to compare **equal** to the stored one — the discipline of
   `check_grid_evidence`. A receipt relabelled to the plain literal scope is refused (an IR
   recording a structure producer must carry this scope, and the reverse). **Tolerance:** none on
   the IR; the IR holds no model float. The only nondeterminism that could matter is a span
   centre within float noise of a model box edge, which would flip an assignment and is then a
   genuine resolve failure (re-run semantics). onnxruntime on CPU is deterministic run to run on
   one machine (measured: identical IR twice per table on every table below); across CPU
   architectures it is not guaranteed, see risks.

6. **Answering: an inferred grid is shown, never testified.** `build_context_block` renders
   `kind=table scope=tsr-inferred-grid-v1`, a line `table rows=R cols=C grid=inferred (rows,
   columns and headers were inferred by a table-structure model and are not verified; cite a value
   as its cell with the cell's exact text, never as a row, column or header)`, then per cell
   `cells.<id> (r,c): <text>` plus `inferred_col="<header> / <header>"` and
   `inferred_row="<stub text>"` where the grid places them. Those two attributes are for aligning
   a value with its labels; they are not `HeaderRef`s and are never citable. `verify.py` is
   unchanged: a `cell` claim must equal the cell's text (`_norm`), cites the cell id and every
   span with the cell bbox and **no** `row` / `col` / `header`; a `row` / `col` / `header` on the
   claim is refused because the grid is not `VERIFIED`; a `quote` on a table block is refused as
   for any gridded table (ADR 0027's quotable rows are a different scope). `SYSTEM_RULES` is
   untouched, so no answer fingerprint changes.

7. **One structure per table.** A table is either an inferred grid or rows, never both. Switching
   the policy on an ingested document recomputes only the object stages (layout and metadata
   replay from the model cache), yields a new processing id whose products coexist with the old
   (different producers), and `publish` moves the `current-*` pointers to the latest.

8. **Missing model ≠ silent rows.** `"tsr"` resolves the recognizer when the semantic adapter is
   built; a missing `PDFSPINE_ONNX_MODELS`, a missing `slanet-plus.onnx` or a missing
   `onnxruntime` / `numpy` / `Pillow` raises `TableStructureUnavailable` (a `ValueError`) whose
   message says, in Chinese, what to download, where to put it and how to switch back to
   `"rows"`. `run_folder_pipeline` checks it in its preflight (`PreflightError`) before any work.
   The same error refuses a resolve on a machine without the model.

9. **Visibility.** `IngestionSummary.table_tsr_grids`, `table_tsr_fallbacks`,
   `table_tsr_fallback_reasons` (counts only; a fallback is also counted in
   `table_row_transcriptions`). Wiring them into `report.md` and the notebook status table is left
   to integration; suggested report line per PDF: ``- `<name>`: N tables with an inferred grid,
   M fell back to rows (reason: count, …)``.

## Guarantees and their strength

- **Anti-fabrication:** every character of a cell is a span's own text; a claimed value must
  equal a cell's text; the grid re-derives from the pinned source and the same weights.
- **Provenance:** a citation names the page, the cell bbox (text-layer extent), the cell id and
  every span.
- **Not guaranteed:** that a value belongs to the printed header the grid puts above it. The grid
  is a model assertion; it is presented as `grid=inferred` and nothing downstream treats it as a
  relation. What improves is the *model's reading*: the alignment is no longer counted by the
  answer model in a tab-joined line, it is printed beside each value.
- **Unchanged:** ADR 0014 (only ink proves a grid; `row` / `col` / `header` stay closed here),
  RESTRICTED isolation, privacy traces (counts only), offline operation (a local ONNX model, no
  network, no LLM), purity (`table_inferred_grid.py` is stdlib-only).

## Licence gate (ADR 0009, ≤ Apache-2.0)

- **SLANet-plus:** PaddleOCR upstream, Apache-2.0; ONNX export by RapidAI (RapidTable v2.0.0),
  Apache-2.0 — as recorded by pdfspine's `_onnx` module and its model survey. The ONNX file
  itself carries no licence metadata (inspected: producer `PaddlePaddle`, only the `character`
  vocabulary key). Not re-verified online in this session; integration should confirm against
  the upstream model cards before shipping weights.
- **Code path:** pdfspine (Apache-2.0); `onnxruntime` (MIT), `numpy` (BSD), `Pillow` (MIT-CMU)
  via `pdfspine[onnx]`, imported lazily; no new base dependency (`test_base_dependencies` holds).
- **TATR:** not used. Its licence (code and the selected checkpoint) is still to be checked
  before it may become an alternative backend.

## Measurements (2026-10-06, Apple Silicon CPU, onnxruntime 1.30, pdfspine 0.11.0)

- **Synthetic statement** (`table_rows_helpers.STATEMENT`): with Helvetica the model returns a
  9 × 3 grid, two header rows, every value in its column; with the authored test font it merges
  the two header lines into one row — accepted with one header row, values still aligned
  (`tests/enterprise_pdf_rag/adapters/test_pdfspine_tsr.py`, runs when the weights are set).
- **Real sample** (the AIA interim-results deck under `data/samples`, read-only; table regions
  proposed by pdfspine's local PP-DocLayoutV3 because the stored AIA release labels no Table, see
  ADR 0027 "Not done"): 21 table detections, 2 with a ruled grid found by `lines`, 19 without:
  **14 inferred grids (74 %)**, 5 fallbacks (`span_unassigned` 3, `no_data_row` 1,
  `grid_too_small` 1). The 14 grids are 9 distinct regions (the layout emitted one table six
  times). In all 14, no printed line was split across grid rows; in 9 every grid row is one
  printed line, in 5 one grid row joins the two lines of a wrapped header. Manual spot check of
  four tables (8 × 11 with two header rows and spanning period headers, 11 × 4, 7 × 4, 5 × 4
  financial tables): every value sits under its printed column header and beside its row label.
  Time per table including the page render: mean 0.14–0.16 s, median 0.15 s, max 0.31 s.
  (Counts and structure only; no sample text is recorded here.)
- **Not measured:** the 249-question encrypted report (not available offline), answer accuracy
  rows vs TSR with a real answer model, and the production (LLM) layout's table regions.

## Integration

- **Weights, one place.** `pdfspine_tsr` looks for `slanet-plus.onnx` beside the ONNX layout
  weights first: `APP_ONNX_LAYOUT_MODEL` ([ADR 0030](0030-onnx-layout-partitioner.md)) names the
  layout file or its directory, and that directory is searched; then `PDFSPINE_ONNX_MODELS`. One
  directory and one setting serve both local models. `pyproject` extra `pdf-onnx`
  (`pdfspine[onnx]`) is the one runtime declaration for both.
- **Notebook.** `UNVERIFIED_TABLE_STRUCTURE = "auto"` (default) is resolved by
  `ingest_mode.choose_unverified_table_structure`: lite -> `"tsr"` when
  `pdfspine_tsr.table_structure_unavailable()` (the ingest's own preflight, without hashing or
  loading the model) is `None`, else `"rows"` with the reason; explicit values as written; full
  always `"rows"`. The status line prints inferred grids / fallbacks to rows with reason codes.
- **Index units.** With `table_row_index_units` on (lite), a pending inferred grid is split like a
  verbatim-rows table ([ADR 0027](0027-unverified-tables-as-verbatim-rows.md) Amendment 1): one
  unit per figure row, its inferred header rows repeated, a row's text its cells' own text. As
  one unit, a 32-row statement on a 13-page report lost its seat to the narrative pages and the
  label question abstained. A proved grid keeps one unit; the index version is unchanged (no
  TSR snapshot existed before this integration).
- **Overlay.** An `"onnx-layout"` Table with no ruled grid takes this branch like any other
  (`tests/enterprise_pdf_rag/adapters/test_feature_overlay.py`); a refused grid falls back to
  rows and row units; the retrieval test bench maps the TSR member to its page.

## Not done

- Real-model acceptance on the 249-question set; a comparison of answer accuracy between the two
  policies.
- Splitting very long statements; rotated tables; tables spanning pages.
- Quoting part of a long label cell (only whole-cell `cell` claims cite an inferred grid).

## Risks

- **Private pdfspine API.** pdfspine 0.11.0 exposes the SLANet-plus session only through
  `pdfspine._onnx` / `_tatr` (`_get_runtime`, `_structure_to_cells`, `_final_cells`); they are
  confined to `pdfspine_tsr._recognize_crop` and pinned with pdfspine. A pdfspine bump must rerun
  the real-model test, and a published TSR snapshot that no longer re-derives is re-run, never
  relaxed (as ADR 0014 item 10).
- **Cross-machine determinism.** Snapshots built on one CPU architecture and resolved on another
  (a Mac build mounted on Databricks x86) may see float differences in the model's boxes; the IR
  is immune unless a span centre sits on a box edge, but that is not proved. Build and serve on
  the same platform, or re-run semantics where the snapshot is served.
- **Weights placement.** Databricks needs `slanet-plus.onnx` on a Volume path, next to the ONNX
  layout weights that `APP_ONNX_LAYOUT_MODEL` names (or under `PDFSPINE_ONNX_MODELS`), before
  ingest *and* before serving (resolve re-runs the model).
- **Precision.** The header rule is a heuristic for rendering; a section title printed before the
  first amount row is shown as a header. The self-check refuses inconsistent grids but cannot
  catch a consistent wrong one (e.g. two columns merged by the model with both values in one
  cell): such a cell is still verbatim and still citable, only its `inferred_col` is too coarse.

## Offline coverage

`tests/enterprise_pdf_rag/processing/test_table_inferred_grid.py` (spans as cell text, model-box
jitter, producer and header rows, inferred headers and row labels, every rejection code, amount
vs year); `tests/enterprise_pdf_rag/adapters/test_tsr_pending_grid.py` (presets and validation,
`"rows"` byte-identical, missing model / runtime refusal and run-folder preflight, ruled table
unchanged, an unruled statement indexed / resolved / rendered / cited by cell with negatives and
separators, near miss and row / col / header / quote refused, re-run inequality and a changed
model refused at resolve, fallbacks with reason codes, run-folder answering the same figure under
both policies, a relabelled receipt refused); `tests/enterprise_pdf_rag/adapters/test_pdfspine_tsr.py`
(the real model, skipped without weights).
