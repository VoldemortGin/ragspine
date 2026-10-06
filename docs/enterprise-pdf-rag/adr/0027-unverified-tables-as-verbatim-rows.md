# ADR 0027: A table with no detected grid is indexed as its verbatim printed rows

Status: Accepted, 2026-10-05. On in the lite ingest preset
([ADR 0025](0025-lite-ingest-mode.md)), off in full. Builds on
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) Decision 7 and
[ADR 0014](0014-ruled-table-grid-proof.md); changes neither. Decision 8's retrieval unit is
refined by Amendment 1 (2026-10-06): a long row table scores as row units, its header repeated.

## Context — the defect

A financial report's statements are mostly **unruled**: no frame, or an outer frame only. The
Table branch (`adapters/semantic_objects._table`) asks pdfspine's `find_tables(strategy="lines")`
for the region's grid; for an unruled or frame-only table that returns **zero** tables (ADR 0014,
measured). `TableExtractionResult.table` is then `None`, the `ir` / `description` /
`qualification` stages are written `UNAVAILABLE`, and `processing_retrieval.eligibility()` skips
the object with *"Table transcription is not verified; only verified tables are retrievable"*.
**The whole statement never reaches the index**: none of its numbers can be retrieved, cited or
answered, although every one of them is ordinary text on the page.

ADR 0014's own rejected alternative says "an unruled table is still transcribed and still
retrievable, it just never claims row / column relationships". That is true of a table whose grid
pdfspine *detects* but cannot *prove* (a `PENDING` grid with a verified cell transcription); it is
not true of a table where nothing is detected at all, which is the common unruled case. This ADR
closes that gap.

## Why ADR 0011 / 0014 excluded it, and why this does not contradict them

The reasons on record are about **structure**, never about the text:

- ADR 0011 Decision 7 admits a Table only through a *literal transcription* bound to the
  table's cells, and its rejected alternative refuses a `VERIFIED` grid because "the grid's rows,
  columns and merges are inferred ... verifying them is a new qualification with no source rule".
- ADR 0014 makes a verified grid mean *ink*: row / column / header citations open only when every
  boundary is a painted rule. A grid inferred from whitespace (`strategy="text"`, a vision model)
  has "no ink to cite" and is out of scope.

Both say: **an unproved structure must not be presented as table evidence**. With no detected grid
there is not even a pending `TableIR` whose cells could be transcribed, so the region fell out
entirely. This ADR admits the region **as text, not as a table**: it claims no cell, column,
header or merge — only that some spans are printed on the same line. That is a geometric fact of
the page, of the same kind as "this span is inside this region", and it is re-derived from the
pinned source on every index and resolve. The answer chain never sees a cell: the member renders
as `fragments.row-N` lines, is cited as a `quote`, and carries no `row` / `col` / `header`.

The stronger worry — *is the reading order inside a table region trustworthy?* — is answered by
not depending on any order the PDF or a model supplies: rows are rebuilt from bounding boxes
(top-down, then left-to-right), and the stored IR is refused unless that rebuild reproduces it.
What remains genuinely unverified is the **meaning** of a position: which column header a number
sits under. That stays the model's reading of the printed rows (the header rows are in the same
member, verbatim), and the block says so in words (`structure=unverified`).

## Decision

1. **One boolean on the ingest plan.** `IngestPlan.unverified_tables_as_rows`
   (`adapters/ingest_mode.py`, ADR 0025) is the only thing the Table branch reads, through
   `SemanticObjectAdapter(plan=...)`. The lite preset turns it on, the full preset leaves it
   off; `ingest_pdf(..., unverified_tables_as_rows=...)` and
   `run_folder_pipeline(..., unverified_tables_as_rows=...)` override the preset for this one
   switch (`None` keeps it). Off, every stage, fingerprint, cache key and processing id is
   byte-identical to before (pinned by tests, including full mode's whole-snapshot digest).

2. **Only the no-grid case.** The branch fires only when `table_detection` found no exact region
   match (`result.table is None`). A detected grid — verified or pending — keeps its ADR 0011 /
   0014 path whatever the switch says; so does a detected grid whose transcription failed (e.g.
   the layout region also owns the caption). A fully ruled table is unaffected (pinned).

3. **Rows are pure geometry** (`ragspine.extraction.evidence.objects.tables.table_rows`, stdlib
   only). The region's own spans (`item.source_span_ids`, page order) are visited by vertical
   centre; a span joins the current line when its vertical overlap with that line's *tallest*
   span is at least `ROW_OVERLAP = 0.5` of the shorter height — scale-free, so it holds for any
   font size, a baseline jittered by a fraction of a point and a raised footnote marker, while two
   lines set solid never overlap that much; anchoring on the tallest span stops a small marker
   between lines from chaining them. Within a line spans sort by left edge (ties keep page order).
   Nothing is merged across lines (a label wrapped over two lines stays two rows), no character
   is added, dropped or normalised, and no column, header or indent is inferred — an indented
   sub-item, a multi-line header and a footnote line are simply rows as printed.

4. **Cells of a row are joined by a tab** (`ROW_SEPARATOR = "\t"`). It is whitespace to BM25,
   to the embedder and to the claim re-read (`_norm` folds whitespace), so `Revenue 1,234,567`
   quotes the row `Revenue\t1,234,567\t1,100,200`; it keeps the row readable; and a span seldom
   carries one, so the row normally splits back into its spans. The IR also stores every span's
   text and id, so the reconstruction never depends on the separator.

5. **Representation: still a `TABLE` record, with its own scope and producer.** Stages `ir`
   (a `TableRowsIR`: `rows[]` of `row_id` / `bbox` / `source_span_ids` / `texts`),
   `description` (rows joined by `\n`, producer `table-rows-verbatim-v1`, `Verification.VERIFIED`
   *of the transcription*, confidence method naming the structure as unverified) and
   `qualification` (a `LiteralQualification` with `scope="verbatim-table-rows-v1"`, no
   `grid_scope`, no `ruling_digest`). The three stages carry the producer
   `<semantic writer producer>:table-rows-verbatim-v1`; `native_crop`, `source_text`, `svg` and
   `table_detection` keep their bytes and fingerprints. `eligibility()` is **unchanged**: the
   required stages succeeded, so the member is admitted; a gridded table and a row table are told
   apart by scope and producer, both auditable in the manifest and in the receipt.

6. **Re-verified on every index and resolve.** `validate_literal_member` switches on the receipt
   scope: producer, confidence, anchor, SVG crop and span set are checked as for any literal
   member, then `check_table_rows` re-groups the named spans from the pinned page's sidecar and
   requires the stored rows character for character, and the description must equal the rows'
   text. A receipt claiming a grid is refused.

7. **Answering: rows are quotes, never cells.** `build_context_block` renders the member as
   `kind=table scope=verbatim-table-rows-v1`, one line
   `table rows=N structure=unverified (each line is one printed row ... cite a row as a quote)`,
   then `fragments.row-N: <row text>`. A `quote` claim on such a block (and only such a table
   block) is verified exactly like a text quote — a verbatim substring of that row — and its
   citation names the row (`fragments.row-N`, the row's bbox, the row text as `quote`) with
   evidence ids `("row-N", <the row's span ids...>)`, so it locates to the page and to every
   span. A `cell` claim, `row` / `col` / `header`, has nothing to bind to and is refused.
   `prompt.SYSTEM_RULES` is untouched, so every answer fingerprint and cached completion stays.
   The prose gate is unchanged: a number in the answer must be in a verified claim's row.

8. **Retrieval unit: one member per table region.** The whole region is one member, exactly as a
   verified table is today, and its index text is the page's contextual header (ADR 0013) above
   all its rows. The header rows are therefore always inside the unit, once, verbatim; no header
   text is ever repeated into another unit, so the question "is this the header or the cited
   row?" cannot arise — a citation is always exactly one row. It adds no file: the record writes
   the same number of assets as a gridded table, which matters on the FUSE-backed runtime.
   The trade-off is granularity: a very long statement is one embedding vector. BM25 scores it
   per term and is the channel short label-and-period questions use (ADR 0018), so recall on
   "<item> <period>" questions does not depend on the vector. Splitting long tables into blocks
   with a repeated header is a possible follow-up if measurements ask for it.

9. **Visibility.** `IngestionSummary.table_row_transcriptions` / `table_row_lines` count the
   tables qualified as rows and their rows (counts only; `0` in an older report).

10. **Switching on an existing document.** Re-running ingest with the switch on recomputes only
    the object stages (the layout and metadata calls replay from the model cache: **zero** new
    calls; the table branch calls no model at all), which yields a new processing id; `qualify →
    index → publish` then embeds the new member (one embedding per new or changed member text) and
    moves the `current-*` pointers atomically. Re-running with it off reproduces the original
    processing id byte for byte, and publishing it moves the pointer back to the original release.
    Snapshots are never migrated in place.

## Guarantees and their strength

- **Anti-fabrication:** every character of a row is a span's own text; the rows re-derive from
  the pinned source; a claimed number must be a substring of the cited row.
- **Provenance:** a citation names the page, the row's bbox and every span of the row.
- **Not guaranteed:** which header a value belongs to. Two columns of years are two printed
  strings; the model reads the association, and the block states the structure is unverified.
  A misaligned header (a header line offset from its numbers) is printed as it is.
- **Unchanged:** RESTRICTED isolation (no sensitivity path is touched), privacy traces (the
  summary adds counts only), offline operation (no SDK, no model, no network in the branch), and
  the core purity rules (`table_rows.py` is stdlib-only).

## Not done

- Restoring a table's structure with a model (or `strategy="text"`): out of scope by request.
- A detected grid whose transcription fails (caption inside the region) still falls out; the same
  row fallback could serve it, but that would replace a `TableIR` in the `ir` stage and needs its
  own decision.
- Rotated text is grouped by its axis-aligned bbox; vertical writing is not handled.
- ~~Chunking long tables with a repeated header.~~ Done by Amendment 1.
- Real-model acceptance on the 249-question set.
- A real-sample count. The stored snapshots under `data/` (read-only, 2026-10-05) hold two Table
  objects in total — both in the synthetic Meridian ingestions, both with a detected (pending)
  grid — and no Table object at all in the AIA release, whose layout labelled none. There is
  therefore no stored object this switch would change; measuring it needs a fresh semantics run
  of a report with unruled statements, which costs live layout calls.

## Offline coverage

`tests/enterprise_pdf_rag/processing/test_table_rows.py` (jittered baseline, raised marker,
right-aligned numbers, wrapped label, ordering, tab join, re-derivation refusals);
`tests/enterprise_pdf_rag/adapters/test_unverified_table_rows.py` (the defect pinned off; an
unruled statement indexed, resolved, rendered and cited row by row with negatives / separators /
total verbatim and a fabricated figure refused; frame-only; a mislabelled paragraph; a ruled table
identical with the switch on; on/off republication with zero model calls; an encrypted PDF;
`run_folder_pipeline` answering from a row).

## Amendment 1 (2026-10-06): a long row table scores as row units with its header repeated

Decision 8 left "splitting long tables into blocks with a repeated header" as a follow-up. A
synthetic interim report measured the need: a 32-row unruled statement under a two-line header,
twelve narrative pages that mention the item and the period, and a running header on every page.
For `Insurance revenue 2024` (a short label-and-period question, BM25-only per ADR 0018) the whole
statement sat at BM25 seat **13** (RRF **27**) — outside the ten answer seats — and the question
abstained, although the row prints the figure. BM25 length normalisation is what buries a
32-row unit under any short paragraph that shares two of the three words.

**Decision.** `IngestPlan.table_row_index_units` (on in lite, off in full;
`run_folder_pipeline(table_row_index_units=...)` overrides the preset) lays a verbatim-rows member's
index text out into **scoring units** — the member, its evidence, its citations and its page-window
line stay exactly what Decisions 5–8 say:

1. **One member, several units** (`MemberText.units`), not one member per row: a row member would
   multiply the assets per table (each with its own ir / description / qualification / svg) on the
   FUSE-backed runtime, move the page window (ADR 0017 is per member) and change what a citation
   names. A citation is still `fragments.row-N` of the one table member, `structure=unverified`.
2. **Header rows** (`processing/index_text.table_header_rows`) are the rows above the first
   *figure row* — a row with a cell that is a printed number (separators, accounting negative,
   sign, currency prefix, percent) and **not a bare four-digit year**, so `US$m 2024 2023` heads a
   column. Conservative: a table opening on a figure row, or a header deeper than
   `MAX_HEADER_ROWS` (4), repeats its first row only.
3. **A unit** is the page's contextual header (ADR 0013), the header rows, and one figure row with
   the label-only rows printed just above it (a wrapped label, a sub-heading); rows after the last
   figure row join the last unit. Every character is the IR's own row text or the page context — no
   character is added. A table with fewer than two figure rows is not split. More than
   `MAX_UNITS_PER_TABLE` (64) units → consecutive groups are merged (a 200-row note becomes 50
   units of four rows), bounding the vectors per table.
4. **BM25** builds its corpus from the units (`LexicalIndex.owners`); a member scores as its best
   unit. **Vectors**: one per unit, stored as one `RetrievalUnitEmbeddings` artifact under one
   stage-cache entry keyed by the description and the units' content (two files per table, not
   per row); the vector channel scores a member as its best unit. Batching (ADR 0026) is unchanged:
   units are flattened into the same `_EMBED_SLICE` slices, a member stored when its last unit is
   back, so the snapshot is identical whether or not the embedder batches.
5. **Snapshot identity.** The switches are named by `RetrievalPlan.index_version`
   (`immutable-cosine-unit-index-v1:table-row-units-v1[+running-lines-unscored-v1]`), part of the
   snapshot id; a mount re-derives the units from the stored rows and refuses a unit count that
   differs from the stored vectors. `processing_store` re-verifies a unit index member by member
   (each member's vectors are its artifact's own, in order). With the switch off the version stays
   `immutable-cosine-index-v1` and every byte is as before (the full-mode store digest is pinned).

**Measured** (offline, synthetic report, `OfflineDescriptionEmbedder`): seat 13 → **1** (BM25),
27 → **1** (RRF); the question goes from *abstained* to *answered* `12,345`, cited on the statement
page as a row quote. Rows alone do it; running lines alone (ADR 0028 Amendment 1) do not.
Visibility: `DraftIndex.row_unit_tables` / `row_units`, and a `report.md` line. Not done: a real
long report (needs live layout calls); a table whose header is offset from its numbers keeps
whatever rows precede the first figure.
Tests: `tests/enterprise_pdf_rag/processing/test_index_text_units.py`,
`tests/enterprise_pdf_rag/adapters/test_lexical_units.py`,
`tests/enterprise_pdf_rag/adapters/test_index_rows_and_running_lines.py`.
