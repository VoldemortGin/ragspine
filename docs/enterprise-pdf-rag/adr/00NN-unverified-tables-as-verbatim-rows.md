# ADR 00NN: A table with no detected grid is indexed as its verbatim printed rows

Status: Proposed, 2026-10-05 (number assigned at integration). Opt-in: off by default. Builds on
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) Decision 7 and
[ADR 0014](0014-ruled-table-grid-proof.md); changes neither.

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

1. **Opt-in switch, one boolean.** `SemanticObjectAdapter(..., unverified_tables_as_rows=False)`
   is the only thing the Table branch reads. `ingest_pdf(..., unverified_tables_as_rows=False)`
   and `run_folder_pipeline(..., unverified_tables_as_rows=False)` pass it through (one line
   each; the lite-mode plan will own it at integration). Off, every stage, fingerprint, cache key
   and processing id is byte-identical to before (pinned by tests).

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
- Chunking long tables with a repeated header.
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
