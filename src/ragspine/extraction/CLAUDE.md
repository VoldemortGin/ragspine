---
covers:
  - src/ragspine/extraction/
verified-against: c78ab20
---

# extraction — agent contract

Auto-loaded when working under `src/ragspine/extraction/`. Keep terse; deep dives go
in `src/ragspine/extraction/docs/`.

## What lives here

Documents → a frozen StyledGrid IR. `extractors/` (xlsx / pptx / pdf, style- &
color-aware), `routing/` (per-page PDF triage), `color/` (color-semantics
registry), `verification/` (dual-channel cross-check → review queue).

## Invariants

- **Extractors are pluggable via `@runtime_checkable` Protocol seams (DI); heavy deps stay
  lazy-imported.** Scanned-PDF OCR injects an `OcrBackend` (`pdf_scanned_extractor`);
  digital-PDF tables inject a `GridExtractor` (`pdf_digital_extractor`, default
  `DoclingGridExtractor`). `GridExtractor` carries a `version` stamped into each fact's
  `extractor_version`, so a swapped parser (Docling → pdfplumber / camelot / …) stays
  distinguishable in provenance. Swap a parser without touching the ingest call site, and
  test the ingestion path offline with a fake — no Docling / PaddleOCR needed.

## Read before editing

- **`GridExtractor.version` is part of the contract.** It is the `extractor_version` written
  to fact lineage; the default `DoclingGridExtractor.version` is `"pdf_digital@1"` (byte-identical
  to the pre-seam stamp). Bump it when the digital parser's output changes.

## Deep dives

<!-- none yet -->
