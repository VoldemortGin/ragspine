---
covers:
  - src/ragspine/extraction/
verified-against: 2672119
---

# extraction — agent contract

Auto-loaded when working under `src/ragspine/extraction/`. Keep terse; deep dives go
in `src/ragspine/extraction/docs/`.

## What lives here

Documents → a frozen StyledGrid IR. `extractors/` (xlsx / pptx / pdf, style- &
color-aware), `routing/` (per-page PDF triage), `color/` (color-semantics
registry), `verification/` (dual-channel cross-check → review queue), `registry.py`
(the `mime → Extractor` dispatch seam: a `@runtime_checkable` `Extractor` Protocol —
`extract(path) → list[StyledGrid]` — + `get_extractor(mime)` / `register_extractor`
over the existing `extract_grids` impls).

## Invariants

- **Extractors are pluggable via `@runtime_checkable` Protocol seams (DI); heavy deps stay
  lazy-imported.** Scanned-PDF OCR injects an `OcrBackend` (`pdf_scanned_extractor`);
  digital-PDF tables inject a `GridExtractor`. **Default is `PdfSpineGridExtractor`
  (`pdf_spine_extractor`, pure-Rust pdfspine, no torch); `DoclingGridExtractor`
  (`pdf_digital_extractor`) is the optional `[pdf-docling]` fallback for ML table
  robustness on messy/borderless tables.** `GridExtractor` carries a `version` stamped into
  each fact's `extractor_version`, so a swapped parser (pdfspine → docling / pdfplumber / …)
  stays distinguishable in provenance. Swap a parser without touching the ingest call site,
  and test the ingestion path offline with a fake — no pdfspine / Docling needed.

## Read before editing

- **`GridExtractor.version` is part of the contract.** It is the `extractor_version` written
  to fact lineage; the default `PdfSpineGridExtractor.version` is `"pdf_spine@1"`; the optional
  fallback `DoclingGridExtractor.version` is `"pdf_digital@1"` (byte-identical to the pre-seam
  stamp). Bump it when the respective parser's output changes.
- **The registry is a behavior-preserving thin wrap.** `registry.py` adds a `mime → Extractor`
  dispatch over the existing `extract_grids` functions; it does **not** change extractor behavior, and
  `routing/pdf_router.py` stays authoritative for the per-page digital/scanned PDF split. Add a new
  format by `register_extractor(mime, extractor)` — **no router edit**; an unregistered mime →
  a typed `UnsupportedFormatError` (a `LookupError`, not a bare `KeyError`).

## Deep dives

- [`docs/extractor-registry.md`](docs/extractor-registry.md) — the `Extractor` Protocol, the
  `mime → Extractor` registry (lazy built-in loaders, `register_extractor` / `get_extractor`,
  typed `UnsupportedFormatError`), and why it's a zero-behavior-change formalization.
