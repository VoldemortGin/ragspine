# ADR 0014: A ruled table's grid is proved from the page's own rulings

Status: Accepted, 2026-09-20. Amends the rejected alternative *A `VERIFIED` `TableIR`* of
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) by supplying the source rule it
said was missing. The literal transcription rule of ADR 0011 Decision 7 is untouched; this ADR
adds a second, independent fact about the same table.

## Context

ADR 0011 rejected a verified table grid in one sentence:

> **A `VERIFIED` `TableIR`.** The grid's rows, columns and merges are inferred and pinned
> `PENDING`; verifying them is a new qualification with no source rule.

That is a statement about a missing rule, not about the source: a ruled financial table paints
its grid, and the painted lines are as much a source fact as a text span. What was missing was a
rule saying *which* painted line proves *which* boundary, and what to do when none does. Until
now `processing/table_models.py` pinned `TableIR` / `TableCell.verification` to `PENDING`
unconditionally, so a cell citation could quote the cell's text but never say which row or column
it sat in — exactly the relationship a financial question asks about.

pdfspine 0.11.0 was measured before the rule was written, and the measurements shape it:

- `page.get_drawings()` returns page-top-left coordinates — the same frame as text spans and as
  `Table.rows` / `Table.cols`. (`get_cdrawings()` is PDF bottom-left and is never used here.)
  A drawing is `{type: 's'|'f'|'fs', rect, color, fill, width, dashes, closePath, even_odd,
  items}` with items `('l', Point, Point)`, `('re', Rect)` or `('c', p0, p1, p2, p3)`.
- `Table.rows` / `Table.cols` are the boundaries **after** snapping, and `find_tables` snaps with
  `snap_tolerance=3.0` by default. Two half-width rules painted at `y=30` and `y=32` become one
  boundary at `y=31`, on which neither stroke lies; a doubled top border (strokes at 30, 32 and
  30 again) becomes `y=30.666…`. Those boundaries are pdfspine's inference, not the page's ink.
- A thin filled rectangle is how real reports paint table rules: `draw_rect(..., fill=…, width=0)`
  0.5pt high arrives as `type='f'`, `width=0.0`, `items=[('re', Rect(...))]`, and `find_tables`
  treats it as a line. A stroked `('re', …)` is one path carrying four edges.
- A frame-only table (outer box, no interior rules) and an unruled one both yield **zero** tables
  from `strategy="lines"`; they never reach a proof stage at all.
- `find_tables(line_max_thickness=3.0)` is pdfspine's own cut-off for what counts as a rule.

## Decision

1. **A ruling vocabulary in `processing/geometry.py` (stdlib, pure).** `Axis`,
   `Segment(path_index, item_index, edge, axis, position, start, end, thickness)` — one
   axis-aligned ruling in page-top-left points, validated finite and ordered — plus
   `coordinate_matches`, `rulings_at`, `covering_segments` (the union of collinear pieces covers
   `[start, end]` with no gap over the tolerance; returns the pieces used, in sweep order, or
   `None`), `segments_crossing` (rulings running *inside* an open interval) and `ruling_digest`
   (sha256 over the page's segments in paint order).

2. **The observations are produced in `adapters/`, where pdfspine lives.**
   `adapters/pdfspine_tables.ruling_segments(page)` walks `page.get_drawings()` and keeps only
   axis-aligned solid strokes (`'l'` items, `width <= LINE_MAX_THICKNESS = 3.0`), thin filled
   rectangles (short side `<= 3.0`, recorded at the mid-line with the short side as thickness,
   edge `re-thin`) and the four edges of a stroked rectangle (`re-top` / `re-bottom` / `re-left` /
   `re-right`). Dashed paths, curves, diagonals and strokes thicker than 3.0 are not rulings.
   `fill_rectangles(page)` returns the non-white filled rectangles for header evidence, in the
   same frame.

3. **`processing/table_grid_proof.prove_grid` is the rule, and it is pure.** It takes the pending
   `TableIR`, the page's segments and pdfspine's snapped `rows` / `cols` — as *inputs to be
   proved*, never as evidence — and returns a `GridProof` or a `GridRejection(reason, cell_id)`:

   - **Preconditions.** The boundary counts match the dimensions, boundaries increase strictly by
     more than the tolerance, the outer boundaries equal the table bbox, and no slot is `UNKNOWN`.
     An irregular grid is not proved.
   - **Rule 1 — every boundary is ink.** Each of the `row_count + 1` row boundaries and
     `col_count + 1` column boundaries carries at least one ruling within `RULING_TOLERANCE`.
     A snapped or averaged boundary fails here, naming the coordinate.
   - **Rule 2 — every cell edge is ruled end to end.** A cell's bbox must equal its boundary
     quadruple, and each of its four edges must be covered by rulings at that position with no gap
     over the tolerance; several collinear pieces are stitched. The pieces used are stored as
     `CellBorderEvidence(top, bottom, left, right)`, each a tuple of
     `SegmentRef(path_index, item_index, edge, p0, p1, thickness)` pointing back into
     `get_drawings()`.
   - **Rule 3 — a merge is proved by absence.** For every interior boundary a merged cell spans,
     no ruling may run inside the cell at that boundary; the spanned boundaries are recorded as
     `MergeProof(interior_rows, interior_cols)`. A merge pdfspine inferred over a rule that is
     actually painted is rejected.
   - **Rule 4 — headers are graded, not assumed.** `HeaderEvidence(kind, strength, rows, cols,
     segments, fills)`: an interior boundary ruled measurably thicker than every other interior
     one (`ruling_thick`) and a leading band covered by a filled rectangle (`fill`) are
     `proved` and cite their segments or rectangles; a leading run of bold-set rows (`font_bold`)
     and "the first row of a table with at least two rows" (`first_row_rule`) are `heuristic` and
     carry no citation. `GridEvidence.proved_header_rows()` / `proved_header_cols()` expose only
     the proved ones.

   The result is pinned as `GridEvidence(rows, cols, ruling_digest, segment_count, tolerance,
   headers, producer="ruled-grid-structure-v1")`.

4. **Two tolerances, two jobs.** `RULING_TOLERANCE = 0.5` answers "is this painted line on this
   boundary, and does it cover this edge" — the same 0.5pt slack `pdfspine_tables._contains`
   already uses against pdfspine's own float and snapping noise. `COORDINATE_TOLERANCE = 1e-6`
   stays what ADR 0011 BUG-1 made it: canonical-vs-canonical equality (cell bbox against its
   boundaries, boundaries against the table bbox, and the axis-alignment test that decides whether
   a stroke is horizontal or vertical at all). The ruling tolerance is stored on the evidence, so
   a re-proof uses the tolerance the proof was made with.

5. **The model stops pinning `PENDING` and starts requiring evidence instead.**
   `TableCell.verification is VERIFIED` ⇔ `border is not None`; `TableIR.verification is VERIFIED`
   ⇔ `grid_evidence is not None`; every cell shares the table's verification; `REJECTED` remains
   impossible. `TableIR.__post_init__` re-checks the stored evidence structurally (boundaries
   against dimensions and bbox, each border on its own edge, a merge proof naming exactly the
   spanned interior boundaries, header rows inside the grid, no unknown slot) — the geometry
   itself belongs to `table_grid_proof`. Both fields default to `None`, so every snapshot written
   before this ADR parses unchanged and stays `PENDING`. `cell_id` never included `verification`
   or evidence in its `content_id` inputs, so a re-proved table keeps its cell ids.

6. **The stage receipt binds the proof, and `resolve` re-proves it.**
   `LiteralQualification` gains `grid_scope` and `ruling_digest`, set by `semantic_objects._table`
   only when the extracted IR is `VERIFIED`, and `literal_qualification._reprove_table_grid`
   refuses a pending grid whose receipt carries either field. For a verified one it re-opens the
   *pinned source PDF*, re-reads the rulings and calls `check_grid_evidence`, which strips the
   stored evidence, proves the grid again from scratch at the pinned tolerance and requires the
   re-proved `TableIR` to compare equal to the stored one. The grid is therefore never trusted
   from the asset; it is re-derived from the source on every resolve, exactly like the literal
   transcription of ADR 0011.

7. **Row, column and header citations open only for a verified grid.** `ContextBlock` carries
   `grid_verification` (from the IR, not the description) and each `CellEvidence` carries its own
   verification plus the `HeaderRef`s that head it (a proved header row heads the cells below it
   in its columns; a proved header column heads the cells to its right in its rows). The table
   line renders `grid=verified|pending`, and only a verified block prints the per-cell
   `row=… col=… header="…"` suffix. `ModelClaim` gains optional `row` / `col` / `header`;
   `verify.py` rejects them as `MODEL_OUTPUT_INVALID` on a non-cell claim, and in `_verify_cell`
   rejects them as `CLAIM_NOT_IN_EVIDENCE` when the block or the cell is not `VERIFIED`, when the
   claimed row / column differs from the IR's, or when the header is not one of that cell's proved
   headers. A verified citation carries `row` / `col` / `header` / `header_cell_id`, and the header
   cell's id joins `evidence_ids`. Retrieval eligibility and the index-text policy are unchanged:
   a pending table stays retrievable and its plain `cells.<id>` citations keep working.

8. **Three text criteria, deliberately.** A cell's text is still compared with `_norm`
   (whitespace-folded **and** casefolded), because the model is quoting content. A cited header is
   compared with `_literal` (whitespace-folded, case kept) — the criterion the literal
   transcription check uses — because a header *names* a column and `Revenue` is not `revenue`.
   Neither is a bare `==`: PDF text arrives with soft line breaks that carry no meaning.

9. **Fail closed, in full.** Nothing is proved, and the table stays `PENDING` with a diagnostic
   naming the reason, when: no table is detected at all (frame-only, unruled, wrong region); a
   boundary carries no ruling within 0.5pt (snapped halves, doubled borders, dashed rules,
   rules thicker than 3.0); a cell edge has a gap; a merged cell has a rule running through it;
   a cell bbox is not on the boundaries; a slot is `UNKNOWN`; or the boundary count disagrees with
   the dimensions. A rejection never downgrades the literal transcription — a pending grid whose
   transcription verified is still a retrievable, citable table.

10. **pdfspine stays pinned at 0.11.0.** The proof compares floats that pdfspine produces, and
    `check_grid_evidence` requires the re-proved table to equal the stored one, so a change in its
    snapping or float output can turn a stored `VERIFIED` grid into a resolve-time failure. An
    upgrade is therefore: bump the pin, run the offline suite (the authored `TableSpec` fixtures
    cover the snapped / doubled / frame-only / unruled / thin-rule / split-segment cases), run the
    real-sample smoke in `tests/enterprise_pdf_rag/adapters/test_pdfspine_tables.py`, and if a
    published snapshot no longer re-proves, re-run `semantics → index → publish` for it rather
    than relaxing the rule. `GridEvidence.producer` exists to carry a future rule version.

## Rejected alternatives

- **Proving against `snap_tolerance` instead of 0.5pt.** Matching at pdfspine's own 3.0pt would
  accept exactly the boundaries it invented: the two half-width rules 2pt apart would "prove" a
  boundary at `y=31` that no ink touches. Lowering `find_tables(snap_tolerance=0.5)` was measured
  too — it changes the detected grid (an extra row), and with it every `cell_id`. The order is
  deliberate: let pdfspine snap at its default, then prove the snapped result against the ink.
- **A new `grid_proof` processing stage.** The evidence belongs to the `ir` asset and travels with
  it; a separate stage would add a required stage name to `eligibility`, invalidate every existing
  snapshot's stage set, and put the proof somewhere a reader of the IR would not look.
- **Letting header heuristics count towards `VERIFIED`.** `VERIFIED` states one thing — the rows,
  columns and merges come from real rules. A bold first row is typography, not structure; if it
  gated verification, an ordinary 1pt-ruled table (the authored default, the synthetic ingestion
  table) would lose its row and column citations for a reason unrelated to its grid.
- **Proving tables found with `strategy="text"` or a vision backend.** Those grids are inferred
  from whitespace; there is no ink to cite. Detection is out of scope here and unchanged: an
  unruled table is still transcribed and still retrievable, it just never claims row / column
  relationships.
- **Migrating existing snapshots in place.** Snapshots are immutable and content-addressed; a
  re-proved `ir.json` is a different asset and therefore a different member and snapshot id.
  Rebuilding is `semantics → index → publish`, on the owner's schedule.

## Consequences and follow-ups

- `processing/table_models.py` and `adapters/pdfspine_tables.py` leave the "untouched" list of
  ADR 0011 BUG-1: both now take part in a verification rule. `COORDINATE_TOLERANCE`'s meaning is
  unchanged; `RULING_TOLERANCE` is new and separate.
- Snapshots published before this ADR (both `data/ingestion` v2 snapshots, the AIA release) still
  parse, mount and resolve: the two IR fields and the two receipt fields default to absent, the
  grid stays `PENDING`, and `ProcessingStore.load`'s member binding is unchanged. Re-running
  `semantics` over the same source turns a ruled table into a `VERIFIED` grid.
- `SYSTEM_RULES` changed, so `request_fingerprint` changes and every cached answer misses once.
- `validate_literal_member` now re-opens the source PDF and re-reads its drawings for each
  verified table member on every `resolve`. Table members are few (at most one per page in every
  sample so far) and v1 accepts the cost; if it becomes a bottleneck, the digest comparison can
  stay at resolve while the full re-proof moves to `build`.
- **Known relaxation:** `get_drawings()` reports path geometry without the clipping stack, so a
  rule that is painted and then clipped away is still counted as ink. This matches what pdfspine's
  own detector sees; correcting it would mean replaying the content stream in the opposite
  coordinate frame.
- **Not addressed:** `strategy="lines"` recall itself is low (21.5% on FinTabNet.c, measured in
  `src/ragspine/extraction/tables/structure.py`). This ADR governs what happens *after* a table is
  detected; improving detection is a separate topic.
- Offline coverage: `tests/enterprise_pdf_rag/processing/test_geometry.py`,
  `test_table_grid.py`, `test_table_grid_proof.py` (the pure rules, including every rejection
  reason); `adapters/test_pdf_ingestion.py` (the `TableSpec` fixtures `DEFAULT_TABLE`,
  `MULTI_HEADER_TABLE`, `FILL_HEADER_TABLE`, `FRAME_ONLY_TABLE`, `UNRULED_TABLE`, `SPLIT_TABLE`);
  `adapters/test_pdfspine_tables.py` (producer, snapped / doubled rejection, real-sample smoke);
  `adapters/test_generic_publication_e2e.py` (receipt, re-proof, tampering);
  `processing/test_context_builder.py`, `answers/test_verify.py`, `answers/test_answer_service.py`
  and `adapters/test_chat_http.py` (the citation chain).
