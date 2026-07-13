---
covers:
  - src/ragspine/extraction/registry.py
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# Extractor registry — mime → Extractor dispatch

Deep dive behind the **`Extractor` (formalize)** row of
[`docs/prd-breadth-via-adapters.md`](../../../../docs/prd-breadth-via-adapters.md) (P0, ⭐🔧).
The PRD is the originating spec; this is the live contract. This was a **behavior-preserving
formalization**: the extractors already existed; the registry only gives them a shared seam.

## The seam

`registry.py` owns exactly one concern: **answer "which extractor handles this format."** It does
**not** extract anything itself, and it does **not** replace the per-page PDF triage in
`routing/pdf_router.py` (that stays authoritative for the digital/scanned split *within* a PDF).

```python
@runtime_checkable
class Extractor(Protocol):
    def extract(self, path: str | Path) -> list[StyledGrid]: ...
```

The IR every extractor emits is `StyledGrid` (see `ir.py`), which carries the lineage
(`source_doc_id` + cell-level `cell_ref` locators) — so the registry is provenance-honest by
construction: it passes a `StyledGrid` list straight through, inventing nothing and dropping nothing.

## Built-ins, registered by mime (lazy, SDK-lean)

`_BUILTIN_LOADERS` maps a normalized mime/suffix key → a **lazy loader** that imports the existing
extractor module only when that format is requested and returns a thin `_FunctionExtractor` wrapping
its module-level `extract_grids`. So `import ragspine.extraction.registry` pulls **zero** extractor
SDKs (docling / python-pptx / openpyxl); the heavy import happens on first `get_extractor(mime)` for
that format.

| mime (and suffix alias) | wraps |
|---|---|
| `application/pdf` · `.pdf` | `pdf_digital_extractor.extract_grids` |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` · `.pptx` | `pptx_styled_extractor.extract_grids` (default `.pptx`) |
| `pptx+pptspine` (`PPTX_PPTSPINE_SELECTOR`) | `pptspine_extractor.extract_grids` (W3c, **opt-in** richer-merges alternative) |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` · `.xlsx` | `xlsx_styled_extractor.extract_grids` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` · `.docx` | `docspine_extractor.extract_grids` (W3b) |

Keys are normalized case- and whitespace-insensitively (`"  Application/PDF "` resolves).

**W3c is additive, not a default swap.** `.pptx` / `PPTX_MIME` keep resolving to `pptx_styled` — that
default already resolves theme/scheme colours, native charts, styled runs and speaker notes, which
`pptspine` 0.1.0 does not (a naïve swap would *regress*). `pptspine` (pure-Rust, richer table merges) is
reached only through the distinct **`"pptx+pptspine"`** selector here, or by injecting
`ingest_file(..., pptx_extractor=PptspineGridExtractor())` in the structured pipeline — both opt-in,
stamping `extractor_version="pptspine@1"`.

## Dispatch + extending without a router edit

- `get_extractor(mime) -> Extractor` — consults the runtime registry first, then the built-in
  loaders. An unregistered mime raises **`UnsupportedFormatError`** (a `LookupError` subclass, so it
  is typed and won't be swallowed by an `except KeyError`), and the message lists the known mimes.
- `register_extractor(mime, extractor)` — adds a new format **with no change to any routing code**;
  runtime registrations take priority over built-ins. A third party registers a format from its own
  package at import time (no core PR). The `extractor: Extractor` annotation is enforced at the call
  boundary by the package-level **beartype** runtime contract (ADR 0004), so a non-`Extractor` is
  rejected before it can enter the registry.
- `registered_mimes() -> set[str]` — all dispatchable keys (built-in + runtime), with no extractor
  module imported.

## Conformance

The registry's dispatch + typed-error contract is bound by
`tests/extraction/test_extractor_registry.py`: a dummy mime registered in-test dispatches to its
extractor through the registry **without touching the router**, an unregistered mime yields the typed
`UnsupportedFormatError`, and the built-in mimes resolve to their `StyledGrid` extractors. The broader
provenance pack over real fixtures (every emitted `StyledGrid` carries `source_doc_id` + a locator)
remains a P1 follow-up.

## What this is not

`.docx` is now a first-class built-in (W3b) via the family `docspine` extractor — tables → `StyledGrid`
(structured channel) and body paragraphs → narrative segments (the narrative channel handles `.docx`
separately in `ingestion/narrative/narrative_extract.py`, not through this grid registry). Adding the
**remaining formats** (HTML/MD/CSV via `unstructured`/`docling`) is the open follow-up — exactly the
commodity surface that should be *adapted*, not authored. The scanned-PDF extractor
(`pdf_scanned_extractor.extract_grids`) is not a built-in registry entry because it requires an injected
`OcrBackend`; it stays behind the `routing/pdf_router.py` per-page plan.
