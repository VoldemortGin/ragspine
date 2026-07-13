---
covers:
  - src/ragspine/extraction/
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# extraction — agent contract

Auto-loaded when working under `src/ragspine/extraction/`. Keep terse; deep dives go
in `src/ragspine/extraction/docs/`.

## What lives here

Documents → a frozen StyledGrid IR. `extractors/` (xlsx / pptx / pdf, style- &
color-aware; **`.docx` via pure-Rust `docspine`, `docspine_extractor`, W3b** — tables →
`StyledGrid`, body paragraphs → narrative segments; **`.pptx` *richer-merges* opt-in via pure-Rust
`pptspine`, `pptspine_extractor`, W3c** — default `.pptx` stays python-pptx `pptx_styled`),
`routing/` (per-page PDF triage), `color/` (color-semantics
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
  stamp); the `.docx` extractor `DocspineGridExtractor.version` is `"docspine@1"`. Bump it when the
  respective parser's output changes.
- **`docspine_extractor` (W3b + W3d) is the family `.docx` extractor** — lazy-imported `docspine`
  (`[doc]` extra, Apache-2.0). Each top-level table → a `StyledGrid` (`sheet="table{M}"`,
  `cell_ref="R{r}C{c}"` on the true grid column via a gridSpan-advancing cursor); `gridSpan`/`vMerge`
  merge spans best-effort into the existing IR (`is_merged_origin` + `merge_span`). **W3d (rich tables
  into the IR, no schema change):** the cell shading colour `cell['fill']` (`<w:shd w:fill>`) populates
  `resolved_rgb` (`_normalize_fill` → `'RRGGBB'` upper / None) so docx colour flows the existing
  SME-gated color-semantics path (`color/`); **nested tables** (`cell['blocks']` `kind=='table'`) are
  emitted as **independent `StyledGrid`s** (`sheet="table{M}.cell{r}_{c}.nested{k}"`, recursive,
  parent-first; the parent grid keeps a breadcrumb warning) — never silently dropped. Wired into
  structured (`ingestion._EXTRACTOR_BY_SUFFIX[".docx"/".docm"]`) **and** narrative
  (`narrative_extract.extract_docx_narrative`); legacy binary `.doc` is intentionally not registered.
- **`pptspine_extractor` (W3c) is the *opt-in, additive* family `.pptx` extractor** — lazy-imported
  `pptspine` (`[ppt]` extra, Apache-2.0). It is **not** the default: a naïve swap would *regress* —
  python-pptx's `pptx_styled_extractor` already resolves theme/scheme colours, native charts, styled
  runs and speaker notes, which pptspine 0.1.0 does not (and pptspine 0.1.0 returns only the first table
  per slide). So the default `.pptx` path stays `pptx_styled`; pptspine is the **richer-merges** option,
  selectable two ways: registry selector `registry.PPTX_PPTSPINE_SELECTOR` (`"pptx+pptspine"` →
  pptspine, while `.pptx`/`PPTX_MIME` stay `pptx_styled`), or structured-dispatch injection
  `ingest_file(..., pptx_extractor=PptspineGridExtractor())`. Each table → `StyledGrid`
  (`sheet="slide{N}_table{M}"`, `cell_ref="R{r}C{c}"`); merge spans from pptspine's resolved
  `col_span`/`row_span` → `is_merged_origin`+`merge_span`. **W3d:** the cell fill `cell['fill']`
  (`a:tcPr` solidFill/srgbClr) populates `resolved_rgb` (same `_normalize_fill` → SME-gated color path);
  theme/scheme colours pptspine 0.1.0 cannot resolve stay `None` (still python-pptx's job — another
  reason pptspine is opt-in). PPTX tables don't nest, so W3d for pptx is fills-only.
  `version="pptspine@1"` stamps fact lineage when selected.
- **The registry is a behavior-preserving thin wrap.** `registry.py` adds a `mime → Extractor`
  dispatch over the existing `extract_grids` functions; it does **not** change extractor behavior, and
  `routing/pdf_router.py` stays authoritative for the per-page digital/scanned PDF split. Add a new
  format by `register_extractor(mime, extractor)` — **no router edit**; an unregistered mime →
  a typed `UnsupportedFormatError` (a `LookupError`, not a bare `KeyError`).

## Deep dives

- [`docs/extractor-registry.md`](docs/extractor-registry.md) — the `Extractor` Protocol, the
  `mime → Extractor` registry (lazy built-in loaders, `register_extractor` / `get_extractor`,
  typed `UnsupportedFormatError`), and why it's a zero-behavior-change formalization.
