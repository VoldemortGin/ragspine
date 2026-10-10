# ADR 0046: Four files per document — the document's model cache is staged too, objects inline up to 8 MiB

Status: Accepted, 2026-10-09. Amends [ADR 0044](0044-staged-object-backend.md) (staged object
backend; its §3 "model cache stays on files" is narrowed, see its Amendment 1) and
[ADR 0036](0036-sqlite-object-backend.md) §6 (the `auto` fallback is no longer silent). Opt-in
like ADR 0044: with `APP_OBJECT_STORE_BACKEND` = `auto` / `sqlite` / `files` nothing changes,
byte for byte. It changes **no hash, no fingerprint, no envelope byte and no published id**.

## Context

On Databricks Workspace files (FUSE) the `auto` probe most likely fails and fell back to the
file layout **silently**: a 300-page document wrote 23–26 k files and the mount dropped
(`Transport endpoint is not connected`). Under `staged` the stores were already single
`store.sqlite` files, but a 71-page run still left on the mount:

- every object over `APP_OBJECT_STORE_INLINE_MAX_BYTES` (256 KiB) as an external file — 44
  page SVGs (264 KB–2.4 MB) and, with model calls, ~400 object SVGs of the same size;
- the document's model cache in the file layout: three files per model call (request record,
  response, context) plus a `.claim` while it runs;
- `-wal` / `-shm` / `*.writer` beside any db opened directly on the mount.

## Decision

1. **Staged objects inline up to 8 MiB** (`STAGED_INLINE_MAX_BYTES`). The registry passes it to
   the staged backend unless `APP_OBJECT_STORE_INLINE_MAX_BYTES` is set explicitly (an explicit
   value, even 262144, still wins; `files` / `sqlite` / `auto` keep 256 KiB). Inline rows are
   already zlib-compressed (ADR 0036: level 6, ≥ 1 KiB, JSON / text / SVG, `encoding` column,
   `raw` rows read as before); the digest is the sha256 of the **raw** bytes, so object
   digests, manifests and processing ids do not move. The PDF stays external
   (`APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES`) and `APP_OBJECT_STORE_MAX_DB_BYTES` still sends
   objects > 16 KiB out once a db passes 400 MiB.
2. **The document's model cache is staged with its store** (`StagedModelCacheBackend`,
   `object_backend/staged.py`). In staged mode `open_backend(cache_dir, "model-cache")` returns
   it when `cache_dir`'s parent is a store root this process is staging (the document's
   `processing/` store — `ingest_pdf` and the tree stage open their client while that store is
   open); anything else, i.e. the root-level answer cache `<ingestion root>/model-cache`, stays
   on the file layout. It is `SqliteModelCacheBackend` with its db in a local working copy
   (`<staging dir>/<sha256(cache dir)[:32]>/model-cache.sqlite`) and the ADR 0044 publish
   machinery (shared as the `_StagedDb` mixin): published whole at every stage boundary as
   `processing/model-cache/model-cache.sqlite`, the same path the `sqlite` backend uses, so a
   later non-staged run reads it. **Not merged into `processing/store.sqlite`**: that would need a
   schema merge and a second writer-lease scope in one db; a separate file is the smaller change
   and keeps the claim / single-flight / 429-cooldown / budget code exactly the sqlite one (all
   in-process, on the local copy; the cross-process claims rows behave as on `sqlite`).
   - "Unpublished writes" is a content signature (row count + latest `created_at` of
     requests / responses / contexts); claims are run-time exclusion and do not count, so a
     replay-only rerun publishes nothing.
   - One instance per cache dir per process, owned by the registry (clients never close a
     model cache); `release_staged(<document root>)` finishes it with the stores.
   - The publisher lease `model-cache.sqlite.publisher` is taken at the first publish.
3. **Clean finish.** The last close (or `release_staged`, or the `atexit` hook, which now
   finishes rather than only commits) commits, closes every connection (`wal_checkpoint(TRUNCATE)`
   on the local copy), and deletes the publisher lease. Published files are `journal_mode=DELETE`
   snapshots; the `-wal` / `-shm` / `.writer` of the working copies live only on the local disk.
   The publisher claim is now taken under a per-instance lock: with `APP_PAGE_CONCURRENCY` > 1
   two page threads writing first could otherwise see their own fresh lease as a foreign live
   holder and fail with `StoreBusy`.
4. **`auto` falling back is visible.** `open_backend` logs one warning per directory and emits
   one trace `event=object_backend_fallback, requested=auto, backend=files,
   failure_code=<probe code>` (`sqlite_wal`, `sqlite_write`, `sqlite_shm`, …; never a path).
   `notebooks/run_folder.ipynb`'s fs-selfcheck prints the backend the run will use and, for an
   `auto` fallback, the failure code (print only).

Result: per document on the mount, **4 files** — `source/store.sqlite`,
`processing/store.sqlite`, `processing/model-cache/model-cache.sqlite`, the PDF
(`source/objects/sha256-sharded/<ab>/<digest>`); plus `answers-audit.sqlite` and the root-level
answer cache once questions are answered. `ingest_mode=lite`; the full-mode review pages
(`processing/runs/<id>/…`) are files by design — generate them on demand with
`export_document_review`.

## Measured (developer Mac, 71-page AIA interim deck, lite, `max_live_calls=0`)

| backend | files on the mount after exit | bytes | logical digest / ids | rerun |
|---|---|---|---|---|
| `auto` = `sqlite` (main b8d4c85) | 49 | 28.47 MB | `f5773a43…` / manifest `7c87bfb1…`, processing `9f37596f…` | — |
| `auto` / `sqlite` (this change) | 49, same names and sizes | 28.47 MB | identical | — |
| `staged` (main) | 48 | 28.27 MB | identical | — |
| `staged` (this change) | **4** | **5.78 MB** | identical | 0 model calls, no published file rewritten |

SVG rows compress ≈ 10.7× (28.7 MB → 2.7 MB in `source/store.sqlite`). A real kill
(`os._exit`) before the rename of the 1st, 2nd or 3rd publish leaves the previous published
version, a `.…staging-*` temporary and the dead process's publisher leases; the rerun takes the
leases over (dead pid), removes the temporary, ends with the same 4 files and the same digest.

## The 500 MB Workspace file limit

Workspace files cap a single file at 500 MB. Estimate for a 300-page full-model document,
**not measured**: page SVGs ≈ 300 × 400 KB = 120 MB raw → ≈ 11 MB; object SVGs at the 71-page
rate (~400 objects, ~0.8 MB average) ≈ 1.3 GB raw → ≈ 125 MB at the same ratio; inline page PNGs
(barely compressible) ≈ 300 × 200 KB = 60 MB. So `processing/store.sqlite` lands around
150–250 MB, under the limit with margin, but object-heavy decks scale with object count, not
pages. `APP_OBJECT_STORE_MAX_DB_BYTES` (400 MiB) stays the guard: past it objects > 16 KiB are
external files again — more files, never a db over the limit. Unity Catalog Volumes have no
such per-file cap; where the ingestion root can live on a Volume, prefer it (the staged working
copy still belongs on `/local_disk0`).

## Weaker / unverified

- Nothing here is measured on Databricks; the FUSE cost of publishing a 200 MB db five times a
  document is the open question ADR 0044 already names.
- A staged model cache is chosen by "the parent store is staged in this process". A client
  opened on a document's cache dir with no store open (no current caller does) gets the file
  layout and would not see the published `model-cache.sqlite`.
- Claims rows can travel inside a published snapshot taken while a call is in flight (only the
  `atexit` path can do that); on another host they are taken over by lease expiry, not pid.
- Switching a document from plain `sqlite` copies only `model-cache.sqlite`; a non-empty `-wal`
  next to it (a writer that never closed — the client does not close its cache) is not merged.

## Rejected alternatives

- **Merge the model cache into `processing/store.sqlite`**: one file fewer, but a schema merge,
  two writer-lease scopes in one db and a different path from the `sqlite` backend.
- **Stage the root-level answer cache**: written by several processes; needs merging, not a copy.
- **Raise the 256 KiB default for every backend**: changes `sqlite` / `auto` files on disk.
