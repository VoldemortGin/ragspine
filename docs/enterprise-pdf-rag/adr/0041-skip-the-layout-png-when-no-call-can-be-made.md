# ADR 0041: Skip the layout PNG when no call can be made; the native SVG stays in the source

Status: Accepted, 2026-10-09. Narrows the last rejected alternative of
[ADR 0024](0024-source-verification-cache.md) ("deferring the layout PNG until the budget allows a
call") to the one case where nothing can be replayed. Leaves the source stage of
[ADR 0010](0010-generic-pdf-ingestion-entry.md) and the evidence chain of
[ADR 0001](0001-architecture.md) unchanged. No schema, no manifest, no request fingerprint, no
stage fingerprint changes.

## Context

Profile of one lite ingest of the 71-page AIA presentation (`data/samples/`,
`layout_policy="deterministic-text-pages"`, `max_live_calls=0`, a fake LLM base URL, this
machine, pdfspine 0.12.0): 20.7 s, of which the source stage's `extract_document` 11.9 s —
`Page.get_svg_image` 10.9 s over 72 calls (every page, plus the focus page again for the
region record) — and the SVG → PNG render of the 56 pages the deterministic layout hands back
to the model 5.9 s, although none of those requests could be sent. A rerun of the same root
(source stage cached) spent 5.9 s of its 8.3 s re-rendering the same 56 PNGs. `get_drawings`
is called 220 times in the first run and costs 0.25 s in all.

## Investigation: where the native SVG lives

- **It is part of the source snapshot.** `ingest_document` puts every page's SVG and names it in
  `PageRecord.svg` (digest + length); the manifest id is the digest of the manifest bytes. The
  source stage is cached by `(PDF sha256, filename, pdfspine producer)` and its manifest is
  reused by every later stage and run of that root.
- **It is part of the evidence chain.** `PageInput.native_svg` is copied into `CanonicalPage`
  (`canonical-source-v1`), whose bytes give the canonical stage fingerprint, which feeds the
  partition and normalization fingerprints (with the SVG ref itself). Every visual view binds
  `native_svg_digest` and `crop_svg_digest` (`visual_semantics`, `figure_reasoning`,
  `source_paint`, the bar / donut / diagram / formula / literal proofs), and requalification /
  publication re-crop the stored SVG and compare digests. The layout request embeds the PNG
  rendered from it, so the model-cache fingerprint depends on it too (ADR 0024).
- **The page's drawing count is in the manifest as well** (`text_layer.drawing_count`), which is
  why `get_drawings` runs on every page; it costs 0.25 s for 71 pages.

### Conclusion: no on-demand SVG extraction

Extracting the SVG only for pages that need geometry would have to leave the ref out of
`PageRecord` / `CanonicalPage` for the others. That is a new source manifest and a new
canonical artifact for every such page — a different source manifest id and different
processing ids for the same PDF than every existing ingestion root holds — and an SVG produced
later would no longer be pinned by the source snapshot but re-derived from the PDF by whatever
pdfspine is installed then. It also does not pay where it was expected to:

- A page-local, source-stage test ("no drawings and no images") selects **0 of 71** pages of the
  sample: every page has at least two drawings (rules, logos, panels).
- What does pay is "the deterministic layout accepted this page": its 15 pages take 5.1 s of the
  11.6 s SVG time (pages 2, 27–29 alone ≈ 4 s, 2 MB SVGs each). That is only known after the
  layout runs, and under `layout_policy="model"` (both library presets) every page's PNG — so
  its SVG — is needed. Making the source manifest depend on the layout policy would break the
  shared source stage and "switching policy sends no new call" (ADR 0025, ADR 0028).

So the SVG stays where it is, byte for byte. Cheaper SVG extraction belongs to pdfspine (the
GIL is released in `get_svg_image`, so pages could be exported on threads with identical bytes),
not to the evidence model.

## Decision

1. `ModelCacheBackend.has_records()` (files: any `requests/*.json`; sqlite: any `requests` row,
   or a legacy `requests/*.json`). Claims, responses and contexts do not count.
2. `JsonCompletionClient.refuses_every_call()`: no live call left **and** `has_records()` is
   false. Then every call that is not `cache_only` can only end in `call_budget_exhausted`: the
   client looks a request up by the fingerprint of its body, and with no record at all there is
   nothing any body could match.
3. `ModelPagePartitioner.partition` asks it before reading the SVG and rendering, and when it is
   true raises `JsonCompletionError("call_budget_exhausted")` itself — the same type and text the
   client raises, so the partition stage records the same `FAILED` outcome (not stage-cached,
   retried next run) and the same `pages_budget_deferred` count.
4. The check sits at the render call site only. Which pages reach the model partitioner (layout
   policy, ONNX, deterministic routing) is untouched.

## Why ADR 0024's objection does not apply

ADR 0024 rejected deferring the PNG because a cached reply is found by a fingerprint over a body
that embeds the image. This ADR skips the render only when the cache holds **no record at
all**, so no reply can be skipped: whenever any record exists — another page's, an earlier run's,
a `.retry-1`, a damaged one — the PNG is rendered and looked up exactly as before. Interrupted
and repeated runs therefore replay what they replayed before; a root whose cache already holds
records behaves exactly as before; a run that spends its last live call starts holding records
and goes on rendering.

## Accepted deviation

`complete_json` validates the request before the budget check, so a page whose PNG would exceed
the 512 000-byte image budget used to fail with `input_budget_exceeded` even with no budget. With
the render skipped it reads `call_budget_exhausted` (one page of the sample, physical page 58),
and that diagnostic is in the processing manifest, so that run's processing id differs. The page
is not cached either way; the first run that can render it — any record or any live call —
records the real code. A second side effect: when an endpoint refused a sampling parameter
earlier in the same process (ADR 0021), the skipped call no longer writes that body's skip
record; the next call that renders writes it.

## Measured (wall clock, monkeypatched timers; same machine, same PDF, fresh root then rerun)

pdfspine 0.11.0 (the version `uv.lock` pins):

| run | total s | `extract_document` s | `get_svg_image` s (calls) | SVG → PNG s (calls) |
| --- | --- | --- | --- | --- |
| first, before | 30.77 | 18.28 | 16.50 (72) | 7.87 (56) |
| first, after | 23.44 | 19.64 | 17.90 (72) | 0 (0) |
| rerun, before | 11.62 | — (cached) | — | 7.86 (56) |
| rerun, after | 2.54 | — (cached) | — | 0 (0) |

pdfspine 0.12.0 (the Context numbers): first 20.68 → 15.26 s, rerun 8.29 → 2.40 s, SVG → PNG
5.87 s → 0. The SVG time moves by ±1.5 s between identical runs (machine noise).

Both versions: same source manifest id; same 15 deterministic pages, 37 objects, 56 failed
partition stages, zero live calls; the processing manifests differ only in physical page 58's
partition diagnostic (the deviation above), so the processing id differs.

## Not done

- On-demand SVG (see the conclusion above) and skipping `get_drawings` (its count is manifest
  content and costs 0.25 s).
- A per-page test ("this page's request was never recorded") would also skip renders on roots
  that already hold records; it needs an index from page to request fingerprint that caches
  written before it do not have, so it was left out.
- `extract_region` re-extracts the focus page (page 1) for the region record: 0.01 s here.

Tests: `tests/enterprise_pdf_rag/processing/test_partition.py` (`test_spent_budget_*`),
`tests/enterprise_pdf_rag/object_backend/test_backend_consistency.py`
(`test_model_cache_has_records_only_once_a_record_is_written` on both backends,
`test_sqlite_model_cache_has_records_sees_legacy_request_files`).
