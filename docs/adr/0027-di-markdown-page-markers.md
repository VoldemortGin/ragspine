---
status: accepted
date: 2026-09-27
---

# ADR 0027 — DI markdown page markers: `DiPage.index` is the true PDF page

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Extends the DI markdown parser (`extraction/di_markdown/parse.py`) and the page-image association
(`ingestion/page_images/source_pdf.py`, [ADR 0025](0025-page-image-trigger-policy.md)). Ported from SuperIndex
`superindex/md_ingest.py` (design note 03).

## Context

SuperIndex's Azure DI extractor (`superindex/extractors/azure_di.py`, `to_markdown`) inserts
`\n<!-- page: N -->\n` at each `pages[].spans[0].offset`, where N is DI's `pageNumber`. When only a range was
analyzed (`pages=5-20`), N is still the page number in the original PDF. The extractor also keeps DI's own
`<!-- PageBreak -->`.

Before this ADR the parser split only on `PageBreak`, so `DiPage.index` was the physical order inside the
markdown. For a markdown that starts at page 5 or skips pages, every locator (`deck.md@page=1`), page tag and
page image was off by the gap, and `source_pdf`'s page-count equality check rejected the PDF.

## Decision

### 1. Marker mode

- **Detection**: the whole text contains a comment whose entire body is `page: N`
  (`<!--\s*page:\s*(\d+)\s*-->`, case-insensitive, same as SuperIndex `md_ingest.py`). Exposed as
  `has_page_markers(text)` and `page_marker_numbers(text)`; `models.py` is unchanged (a new field would change
  `repr` and break the byte-identical guarantee below).
- **Page number cap**: `MAX_MARKER_PAGE = 10000`. Gaps are filled with empty pages, so without a cap one line
  `<!-- page: 99999999 -->` would build a hundred million pages (hours, gigabytes) on the upload / ingest path.
  A marker above the cap is not a page marker: it is stripped like any unknown comment, and if no marker within
  the cap remains the text takes the PageBreak / no-marker path. Detection and `page_marker_numbers` obey the cap.
- **`page: N` wins**: in marker mode `PageBreak` does not split pages; it is dropped as an unknown comment.
- **`index == N`, gaps become empty pages**: page N sits at position N; a page with no marker is an empty
  `DiPage` (`blocks=()`, `number=index`). Page count = max N. Every consumer (narrative locator
  `page={page.index}`, page tags, page images rendered from PDF page p, `page_key` `@page=N`) is correct without
  a change. An empty page yields no narrative segment and one `low_text` row in `page_tag`.
- **`PageNumber`** is unchanged: it sets `number` only, never the split.
- **Abnormal numbers**: `page: 0` counts as 1; a repeated or out-of-order N is bucketed by N, and a bucket keeps
  document order. The heading stack carries over in page order. Parsing never raises.
- **Leading content** (before the first marker) belongs to the first marker's page.
- **Inline marker**: the line is cut at the marker — text before it stays, text after it moves.
- **Marker inside `<table>`** (the `<table` tag starts a line and pairs with a `</table>`; tables are paired one by
  one with a stack, so a table that never closes is given up on its own and does not stop later tables): the table
  is parsed once, each cell is assigned to the page its first text is on (its start tag when empty), and the table
  is rewritten as one table per page, with the markers kept in place. Each page's grid is built directly and handed
  to the block parser, so the split is O(cells + pages).
  - **Header**: the header block (`header_row_count` rows) is repeated on every page. A rowspan reaching out of the
    header block into data rows is clipped to the block in the copy. This replaces the earlier "header copied
    verbatim" rule: copying verbatim a `<th rowspan>` that also covers data rows would drag the page's first data
    rows under it, shift their columns and change `header_row_count`.
  - **Data rows** stay in their columns. A rowspan anchor coming from an earlier page is re-emitted in the page's
    first row with the remaining row count; a cell of the same row that belongs to another page (a marker in the
    middle of a `<tr>` or a `<td>` / `<th>`) leaves an empty placeholder, always a `<td>` so it cannot turn a data
    row into a header row. The exception is the row label: the row's leading consecutive non-numeric `<th>` cells,
    or without them its column-0 cell if it is not value-like, is copied to the same position on each later page of
    that row; a label that is itself a rowspan anchor goes through the same re-emission, once per page.
    "Value-like" is `_is_value_like` in `parse.py`, a private stdlib function (NFKC; sign and Unicode minus;
    accounting parentheses; currency prefixes such as `$`, `US$`, `HK$`, `RMB`, `¥`, `€`, `£`; unit suffixes such as
    `bn`, `mn`, `m`, `k`, `x`, `%`, `pp`, `bps`; footnote stars; thousands commas or spaces; decimals; a year counts
    as a value). It is not shared with `extraction/evidence/`, which stays a separate line, so `di_markdown` keeps
    its stdlib-only contract.
  - So numeric cells are neither repeated nor lost, except that a numeric cell with a rowspan crossing pages
    appears once on each page, like any cross-page rowspan anchor; this matches linearization, where every row it
    covers repeats its value. A row cut across pages has its label once on each page while its numbers are not
    repeated.
  - A page left with no data row (for example a marker right after the header) gets no header-only table; the
    caption goes with the first page that has one. Other comments inside the table (PageHeader / PageFooter /
    PageNumber) stay on their page.
  - A table **without** a matching `</table>` is not rewritten and keeps the existing "an unclosed table runs to the
    page end" rule, so it cannot swallow the text of later pages.
  - `<figure>` is not repaired. A table inside a figure whose `<table` starts a line is still split per page like any
    other; the cut figure then runs to the end of its first page (the first half of the table HTML becomes figure
    text), the later page gets a Table block, and the leftover `</figure>` / `<figcaption>` lines become a
    paragraph. A table written on the same line as `<figure>` is not split.

### 2. Provenance

In marker mode the locator is the true PDF page (`deck.md@page=5#para…`), and image parts carry that page.
Markdown without markers keeps physical order.

### 3. `source_pdf` validation

- **PageBreak / no markers**: PDF page count must equal max `DiPage.index` — unchanged.
- **Marker mode**: `max(index) <= PDF page count`, since a partially analyzed markdown covers fewer pages than
  the PDF. A marker beyond the PDF's last page is an explicit `SourcePdfError` (no silent truncation).
- **Compensation** (marker mode only): relaxing to `<=` weakens the wrong-pairing guard, so the sidecar
  `<stem>.meta.json` is checked. The two producers give a page count different meanings, so they are read by
  separate functions (`read_sidecar_facts` → `SidecarFacts.analyzed_pages` / `.pdf_total_pages`), never by a
  fallback order:
  - **SuperIndex** (`azure_di.extract_corpus`): integer `pages` = the number of pages *analyzed*
    (`describe(result)`). It must equal the number of **distinct** `page:` numbers in the markdown
    (`page_marker_numbers`). This checks that the markdown matches the extraction, not the PDF total, so a
    partial analysis (PDF 20 pages, markers 5..7, `pages: 3`) passes.
  - **ragspine** (`pdf_to_di_markdown`): `page_count` = the **total** pages of the source PDF. It must equal the
    PDF's page count, with no partial-analysis relaxation. Its `pages` is a per-page list and is not a count.
  - **PDF sha256**: `source_pdf_sha256` (also accepted: `pdf_sha256`, `source_sha256`; hex, case-insensitive)
    must equal the actual PDF's sha256. Neither producer writes it yet (SuperIndex will in its next version);
    `source_pdf_sha256` is the name new producers should use.
  - A missing field skips that check; no sidecar skips them all. A sidecar that exists but is not valid JSON or not
    a JSON object, or a checked field of the wrong type (`page_count` / integer `pages` not an int, a sha256 field
    not a string), an integer `pages` <= 0, or an empty sha256 (reported as empty, not as a mismatch) is a
    `SourcePdfError`, as `resolve_source_pdf` already does for a corrupt sidecar. ragspine's list `pages` is
    allowed and is not an analyzed count. A UTF-8 BOM is not accepted (unchanged).
  - **Known limit, blank pages**: the SuperIndex extractor inserts a marker only for a page with content
    (`azure_di.py` `to_markdown` skips pages without spans), so for a PDF with blank pages `pages` exceeds the
    distinct marker count and the check fails. Remedy: re-extract with a SuperIndex version that also marks blank
    pages, or drop `pages` from the sidecar. The extractor fix is scheduled separately.

## Compatibility and migration

- Without a `page:` marker the parser takes the old `PageBreak` path: byte-identical, frozen by
  `tests/extraction/di_markdown/test_legacy_parse_snapshot.py` (sha256 of `repr(parse_di_markdown(x))` taken at
  `main@a0fe837` over PageBreak-only and marker-free inputs, plus the optional 71-page sample).
- Affected stored data: only markdown **with** `page:` markers whose pages do not start at 1 or have gaps. Their
  old locators are physical order; they could not pass the old equality check, so they have no page images.
  Narrative ingest skips by `file_hash` and page tags are signed by md sha256 + `PAGE_TAGS_VERSION`, so neither
  refreshes itself and there is no `--force`. To migrate, delete those documents' rows from `narrative_doc` and
  re-ingest, or build a new store. `PAGE_TAGS_VERSION` is not bumped: tags and chunks should move together.
  Markdown without markers is unaffected.

## Consequences

- Frozen by `tests/extraction/di_markdown/test_parse.py` (marker cases, page cap with a time guard, table split:
  numeric cells, column alignment, header / data rowspans, markers inside `td` / `th` / `tr`, unclosed tables),
  `tests/ingestion/page_images/test_source_pdf.py` (each validation rule, both sidecar sources, sha256, corrupt
  and mistyped sidecars),
  `tests/service/test_page_images_switch.py::test_facade_page_markers_use_true_pdf_pages` (8-page PDF, markers
  5..7: locator `@page=5`, stored image = PDF page 5, `ask` attaches `(deck.md, 5)`) and the legacy snapshot.
- Page images still render every PDF page, including pages the markdown does not cover; rendering only covered
  pages is a possible follow-up.
- Until producers write `source_pdf_sha256`, a marker-mode pairing without a ragspine `page_count` is guarded
  only by `max(index) <= PDF pages` and the analyzed-page consistency check.

## Alternatives considered

- **Map true page → page without empty pages.** Needs a new `DiPage` field and changes in four consumers.
- **Reopen the cut table textually (SuperIndex `_repair_html_tables`).** Re-opening `<table>` + header rows +
  open `<tr>` / `<td>` keeps cell text, but a rowspan crossing the cut or reaching out of the header still shifts
  columns; rewriting from the parsed grid keeps positions exact.
- **Move a marker inside a table to after `</table>`.** Simpler, but the rest of the table's numbers would be
  recorded on the previous page.
- **Keep the equality check in marker mode.** Rejects every partially analyzed markdown.
