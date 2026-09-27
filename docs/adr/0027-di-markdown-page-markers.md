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
  `has_page_markers(text)`; `models.py` is unchanged (a new field would change `repr` and break the
  byte-identical guarantee below).
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
- **Marker inside `<table>`**: if the table is still open at the marker, the next segment goes to a different
  page and is not empty, the next page starts with `<table>` plus the original table's leading consecutive
  all-`<th>` rows, copied verbatim. The first half keeps the existing "an unclosed table runs to the page end"
  rule. So every data row is recorded on the page it is really on, and the data cells of the halves together
  equal the original table's, with none repeated or lost. A marker inside `<figure>` is not repaired.

### 2. Provenance

In marker mode the locator is the true PDF page (`deck.md@page=5#para…`), and image parts carry that page.
Markdown without markers keeps physical order.

### 3. `source_pdf` validation

- **PageBreak / no markers**: PDF page count must equal max `DiPage.index` — unchanged.
- **Marker mode**: `max(index) <= PDF page count`, since a partially analyzed markdown covers fewer pages than
  the PDF. A marker beyond the PDF's last page is an explicit `SourcePdfError` (no silent truncation).
- **Compensation**: relaxing to `<=` weakens the wrong-pairing guard, so when the sidecar `<stem>.meta.json`
  records the analyzed page count, it must **equal** the PDF page count. The field is `page_count` (ragspine's
  `pdf_to_di_markdown` sidecar) or else `pages` (SuperIndex `extract_corpus`, from `describe(result)`), and only an
  integer counts (ragspine's `pages` is a per-page list and is ignored). With no sidecar, an unparseable one, or
  no such field, only `<=` applies.

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

- Frozen by `tests/extraction/di_markdown/test_parse.py` (marker cases, table split proof),
  `tests/ingestion/page_images/test_source_pdf.py` (each validation rule),
  `tests/service/test_page_images_switch.py::test_facade_page_markers_use_true_pdf_pages` (8-page PDF, markers
  5..7: locator `@page=5`, stored image = PDF page 5, `ask` attaches `(deck.md, 5)`) and the legacy snapshot.
- Page images still render every PDF page, including pages the markdown does not cover; rendering only covered
  pages is a possible follow-up.
- A SuperIndex sidecar from a partial analysis (`pages` = analyzed count < PDF pages) fails the compensation
  check; such a pairing needs the sidecar field removed or corrected.

## Alternatives considered

- **Map true page → page without empty pages.** Needs a new `DiPage` field and changes in four consumers.
- **Move a marker inside a table to after `</table>`.** Simpler, but the rest of the table's numbers would be
  recorded on the previous page.
- **Keep the equality check in marker mode.** Rejects every partially analyzed markdown.
