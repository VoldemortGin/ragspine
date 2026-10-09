# ADR 0040: `extract_document` extracts pages on a thread pool

Status: Accepted, 2026-10-09. Amends `PdfspineDocumentAdapter.extract_document`
(`adapters/pdfspine_document.py`). Nothing on disk changes: every page, span id, warning,
source manifest id and SVG byte is identical to the serial path, and with
`APP_PDF_EXTRACT_WORKERS=1` the code runs the old serial loop, in the calling thread.

## Context

After [ADR 0041](0041-skip-the-layout-png-when-no-call-can-be-made.md) the first ingest of the
71-page AIA sample spends about 90 % of `extract_document` in `Page.get_svg_image` (72 calls,
11 to 17 s). The loop opened the document once and walked the pages serially.

## Decision

`extract_document` maps `_checked_page` over `range(page_count)` on a `ThreadPoolExecutor`
(`pdf-extract-*` threads) sharing the **one** opened `Document`; `Executor.map` yields in page
order, so the tuple of `PageExtraction`s is filled by page index and the first failing page (by
index) raises the same `ValueError("Page index N extraction failed: …")` with the same
`__cause__` as serially. The document is closed after the pool has drained.

- Setting: `Settings.pdf_extract_workers` (`APP_PDF_EXTRACT_WORKERS`, 1..64, default unset =
  `min(4, os.cpu_count())`), capped at the page count. The constructor argument
  `PdfspineDocumentAdapter(page_workers=...)` overrides it (tests).
- Whole-page work is parallel (`get_text("dict")`, `get_svg_image`, `get_drawings`, span
  validation), not only the SVG export.
- Sharing one `Document` is safe: pdfspine's `Document` / `Page` are frozen classes with only
  `&self` methods. pdfspine 0.11.0 (the `uv.lock` pin) already releases the GIL in
  `get_svg_image`: 24 pages, 3.63 s serial, 1.90 s on 2 threads, 1.22 s on 4, for a shared
  document and for one `open` per thread alike, with identical SVG digests. So no per-thread
  `open` is needed.

## Measured (71-page AIA sample, pdfspine 0.11.0, this machine)

| workers | `extract_document` s |
| --- | --- |
| 1 (serial) | 14.2 |
| 2 | 6.4 |
| 4 | 3.3 |
| 8 | 2.3 |

At every worker count the repr of the `DocumentExtraction` hashes identically, and a full
`ingest_pdf` (default workers) gives the same `source_manifest_id`
(`7c87bfb1…6316247`) and `processing_id` as before; first-ingest wall 15.7 s → 6.4 s.

## Risk

Threads multiply peak memory by up to the worker count of in-flight page renders. A future
pdfspine that stops releasing the GIL would make the pool a no-op, not wrong.

Tests: `tests/enterprise_pdf_rag/adapters/test_pdfspine_document_parallel.py`.
