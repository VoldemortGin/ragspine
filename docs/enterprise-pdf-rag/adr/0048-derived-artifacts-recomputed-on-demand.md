# ADR 0048: Derived object artifacts are recomputed on demand — only their digest is stored

Status: Accepted, 2026-10-10. Narrows what [ADR 0029](0029-sharded-store-layout-and-self-healing.md)
Amendment 2 / [ADR 0036](0036-sqlite-object-backend.md) write for three object stages; leaves the
stage fingerprints, the envelopes, every manifest, every processing / published id and every
model request fingerprint unchanged. Opt-in except under `staged` ([ADR 0044](0044-staged-object-backend.md)
/ [0046](0046-four-files-per-document.md)): `APP_PERSIST_DERIVED_ARTIFACTS` unset keeps the
bytes on `files` / `sqlite` / `auto`, byte for byte.

## Context

A full object run of the 71-page AIA deck stored 480 `image/svg+xml` objects (184 MB raw, 85 % of
everything written) for 37 objects: `crop_native_svg` wraps the **whole** page SVG in a viewport,
so every crop is page-sized, and each object carries several (`native_crop`, `svg`, one per
producer variant). The PNG renders (`model_render`) are small (50, 0.9 MB). The page SVGs in the
source store are the evidence root and are not touched here.

## Who reads the bytes (inventory)

| stage | written by | reads the **bytes** | recomputable byte for byte? |
|---|---|---|---|
| `native_crop` | `semantic_objects` (every non-text kind) | `semantic_objects._unavailable` (copies it to `svg`); review export | yes: `crop_native_svg(page SVG, page size, item.bbox)`, pure Python string work |
| `svg` (text / list / group / table / diagram / formula / image, unavailable chart) | `object_processing`, `semantic_objects` | publication proofs (`literal_` / `diagram_` / `formula_qualification`, `diagram_publication`) compare it with their own fresh crop; `visual_requalification` feeds a diagram's to `qualify_diagram`; review export | yes: same crop |
| `svg` (prepared chart) | `semantic_objects._chart` | `chart_publication` / `bar_publication` (answer-time `resolve_*` too) and chart requalification compare it with a fresh `prepare_figure`; review export | yes: crop + `<metadata>` of the page spans inside the bbox, deterministic |
| `model_render` | `semantic_objects` (chart, visual kinds) | **nobody but the review export**; the model sees a PNG rendered in the same call, and `model_view` already pins `render_digest` + `renderer_fingerprint` | yes, with the same resvg-py / resvg (the version is in `renderer_fingerprint`) |
| `model_view` | `semantic_objects` | proofs parse it (bbox, page context, digests) | kept: small JSON, not derived from bytes alone; it pins the derived digests |

The model-cache request fingerprint hashes the request body, which embeds the PNG an ingest
renders fresh before every call (replay included); no stored PNG is ever read for it.
Measured: every derived stage of the mixed seven-page fixture (chart, diagram, formula, image,
table, text objects) and all 45 of the 71-page
deck under `deterministic-text-pages` recompute twice to their stored SHA-256.

## Decision

1. `ProcessingStore.cache_output(..., derived=True)` — passed only by those three stages — writes,
   when `persist_derived` is off, the stage entry with the **same envelope and outcome** and no
   product, no object; plus one backend record `derived-artifacts/<sha256>.json`
   (`derived-artifact-v1`: digest, media type, length). An entry written earlier with its
   bytes is kept as it is (no marker).
2. A marked digest is whole without bytes: the stage-cache lookup accepts it, `save_draft` /
   `load` / the retrieval dependency sweep skip it (`ProcessingStore.held`). Its integrity is
   checked where it is consumed, never assumed.
3. Every reader goes through `derived_artifacts.derived_artifact(assets, ref, recompute)`: an
   unmarked ref is read exactly as before; a marked one is recomputed (the consumer's own fresh
   crop / figure, or `object_stage_bytes` from the page SVG + the page layout item) and handed
   out **only** if length and SHA-256 equal the ref. Otherwise `DerivedArtifactDrift` (a
   `ValueError`) and a trace `event=derived_artifact_drift, media_type=…` — a tampered store or
   a changed crop / renderer version, never silently used.
4. `APP_PERSIST_DERIVED_ARTIFACTS`: explicit value wins; unset → `false` under
   `APP_OBJECT_STORE_BACKEND=staged`, `true` otherwise (`persist_derived_default`), the same
   "by mode when not set" rule as the 8 MiB inline default of ADR 0046.

Anti-fabrication is untouched: no number comes from a recomputed crop or render; the proofs that
read them compare geometry and verbatim spans exactly as before.

## Measured (developer Mac, max_live_calls=0, fake base URL)

71-page deck, `layout_policy="deterministic-text-pages"` (a model layout cannot run at 0 calls:
`nothing_to_index`), 45 derived stages (37 `svg`, 8 `native_crop`), 32.1 MB raw:

| arm | processing db | files / bytes under the root | ids | rerun |
|---|---|---|---|---|
| lite, sqlite, persist | 1.82 MB | 72 / 56.2 MB | processing `ee05ff99…`, published `f0f3e85a…` | 0 calls, same id, no repair |
| lite, sqlite, no persist | 1.61 MB | 51 / 29.2 MB | identical | identical |
| lite, staged, persist (explicit) | 3.74 MB | 4 / 8.55 MB | identical | identical |
| lite, staged, default (no persist) | **1.69 MB** | **4 / 6.50 MB** | identical | identical |
| full, sqlite, persist | 1.75 MB | 1014 / 273.8 MB | processing `e051e285…`, published `e2335893…` | 0 calls |
| full, sqlite, no persist | 1.54 MB | 993 / 246.8 MB | identical | identical |

`export_document_review` writes the same 433 files with the same bytes in both arms of a mode.
On the mixed seven-page fixture (chart, diagram, formula, image, table, text) the published id
and the request digest equal `FULL_PUBLISHED_ID` / `FULL_REQUESTS_DIGEST`, the full-mode review
pages are byte-identical, mount + every chart member's answer-time requalification pass.
`FULL_STORE_DIGEST` holds in the default arm only: the no-persist arm stores fewer bytes by
design (staged tests that pin it now set `APP_PERSIST_DERIVED_ARTIFACTS=true`).

## Weaker / unverified

- The full object chain with real model calls (the 184 MB case) is not re-measured here; the
  estimate is that every object SVG goes, i.e. ~85 % of the bytes.
- Readers pay the recompute: a crop is a string wrap of the page SVG (ms); a chart's figure SVG
  re-runs `prepare_figure`, which also renders its PNG (tens of ms per chart, only on mount /
  publish / chart answers / export).
- A resvg upgrade makes every marked `model_render` (export only) refuse with
  `DerivedArtifactDrift`, as it already made a persisted run refuse the conflicting stage output.
- Switching a root from no-persist to persist does not back-fill bytes for entries already
  written; switching the other way leaves the stored bytes in place.

## Rejected alternatives

- **A field in the stage envelope** marking "no bytes": changes the envelope bytes and their
  digests between the two modes; the record keyed by artifact digest leaves them identical.
- **Recompute only, ignore stored bytes**: drops the "corrupted evidence is refused on read"
  check for stores that keep the bytes.
- **Not persisting `model_view`**: small JSON that pins the digests and the renderer; keeping it
  costs nothing.
