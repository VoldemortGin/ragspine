# ADR 0047: One sqlite file per document — the stores, the model cache and the PDF in `document.sqlite`

Status: Accepted, 2026-10-10. Amends [ADR 0046](0046-four-files-per-document.md) (four files per
document) and [ADR 0044](0044-staged-object-backend.md) (staged object backend) for the document
stores only. Opt-in like both: with `APP_OBJECT_STORE_BACKEND` = `auto` / `sqlite` / `files`
nothing is written differently, byte for byte. It changes **no hash, no fingerprint, no envelope
byte and no published id**. [ADR 0036](0036-sqlite-object-backend.md) rejected "one db per
ingestion root" (many writer processes, one lease); this is one db per **document**, which has
exactly one writer process (the holder of its publisher lease) — the reason does not apply.

## Context

After ADR 0046 a document under `staged` leaves four files on the mount: `source/store.sqlite`,
`processing/store.sqlite`, `processing/model-cache/model-cache.sqlite` and the PDF copy
(`application/pdf` is always external). Each stage boundary publishes up to three of them. The
decision is one file per PDF.

## Decision

1. **`StagedDocument`** (`object_backend/staged.py`). In staged mode `open_backend` resolves
   `<doc>/source`, `<doc>/processing` (object role) and `<doc>/processing/model-cache`
   (model-cache role) to **three scopes of one `StagedDocument`** keyed by `<doc>`
   (`document_root_of`: the store root is named `source` / `processing`; the cache dir is
   `processing/model-cache`). Its local working copy is
   `<staging dir>/<sha256(doc)[:32]>/document.sqlite`, published whole as
   `<doc>/document.sqlite` with the ADR 0044 machinery (`_StagedDb`: restore, content signature,
   publisher lease `document.sqlite.publisher`, temporary + `os.replace`, `atexit` finish). One
   commit per document per boundary. Any other staged store root keeps ADR 0044's one db per root.
2. **Scopes in one schema** (`sqlite.DOCUMENT_SCHEMA`). The content-addressed `objects` table is
   **shared** (same digest → one row of bytes); each object scope records membership in
   `<scope>_object_refs` and reads through the view `<scope>_objects`, so a scope never sees
   another's objects (`get_object`, `object_names`, `verify_many`, `pin` all read the view).
   `stage_cache` / `pointers` / `records` are per scope (`<scope>_stage_cache`, …). The model
   cache keeps its own tables (`requests` / `responses` / `contexts` / `claims`) — one per
   document. A digest new to a scope but already stored by the other returns `placed` (a new
   membership row, published at the next commit). `SqliteBackend(core=, scope=)` and
   `SqliteModelCacheBackend(core=)` are the only seam changes; without them every table name
   and SQL text is the old one.
3. **One core per document.** The three scopes share one `_SqliteCore`: one connection per
   thread, one reentrant transaction scope (a source write inside a processing transaction, or a
   model-cache write inside either, joins the same transaction instead of waiting on its own
   write lock), one process-scoped writer lease on the local db. The model cache therefore holds
   its writer lease per process here, not per transaction as on `sqlite` — a document's cache is
   written only by its publisher.
4. **The PDF is in the db.** In a document db `application/pdf` is inlined up to
   `STAGED_PDF_INLINE_MAX_BYTES` (64 MiB, or the inline cap if that is higher); a larger PDF
   is an external file under `source/objects/sha256-sharded/` as before. An explicitly set
   `APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES` still wins (the default is no longer applied in a
   document db). PDFs are not compressed (not a compressible type), and the digest is of the raw
   bytes, so the source manifest does not move.
   **Path consumers:** none needs a file. Every source PDF is opened from bytes
   (`pdf_password.open_pdf(bytes)` — pdfspine, `shared_pdf`, `pdf_ingestion`), read through
   `LocalDocumentStore.get`. `content_path` / `asset_path` are drift-watch helpers that already
   raise `LookupError` for a db-resident object on `sqlite` ("pin it instead"), and the inline PDF
   is just such an object; the full-mode review export writes `source.pdf` from `store.get`. So
   no temporary copy is materialized.
5. **Old layouts are a `layout_mismatch`, not migrated.** `StagedDocument` refuses a document
   directory holding any of `source/store.sqlite`, `processing/store.sqlite`,
   `processing/model-cache/model-cache.sqlite` (ADR 0044 / 0046 staged, or the `sqlite` backend):
   `LayoutMismatch` (a `BackendUnavailable`, fixed text with the code `layout_mismatch`, no
   path) telling to delete that document directory and rerun, or to switch back to the backend
   that wrote it. Conversely `files` / `sqlite` / `auto` refuse a document directory holding a
   `document.sqlite` with the same code — they would otherwise see an empty store. Chosen over a
   one-time import because the four-file layout existed for one day on an opt-in path, and an
   import would have to delete the old files on the mount to reach one file. The file layout
   (`objects/…`, `stage-cache/…`, written by `auto` falling back on FUSE) is still read through,
   as on `sqlite`.
6. **Store roots are created on the first write.** The db no longer lives under `<doc>/source` /
   `<doc>/processing`, so a scope makes its directory on its first write (full-mode review pages
   and external objects go there). A lite run leaves those two directories empty.

Result: per document on the mount, **one file** — `document.sqlite`.

## Measured (developer Mac, 71-page AIA interim deck, lite, `max_live_calls=0`, fake base url)

| backend | files after exit | bytes | manifest / processing / store digest | rerun |
|---|---|---|---|---|
| `auto` (main e91a3ff) | 49 | 28 465 178 | `7c87bfb1…` / `9f37596f…` / `f5773a43…` | — |
| `auto` (this change) | 49, same names and sizes | 28 465 178 | identical | — |
| `staged` (main e91a3ff) | 4 | 5 779 914 | identical | — |
| `staged` (this change) | **1** (`document.sqlite`) | **5 701 632** | identical | 0 model calls, `document.sqlite` mtime_ns unchanged (also after wiping the staging dir: restored, not republished) |

`document.sqlite`: 221 object rows (145 source members incl. the 1 028 554-byte PDF inline, 76
processing members, no digest shared by both scopes on this deck), 72 stage-cache rows,
`journal_mode=delete`, no free pages. A real kill (`os._exit`) before the rename of the first
publish leaves a `.document.sqlite.staging-*` temporary and the dead process's publisher lease;
the rerun takes the lease over, removes the temporary and ends with the one file and the same
digest (on this deck a lite run publishes once). The mixed fixture keeps `FULL_STORE_DIGEST` /
`FULL_REQUESTS_DIGEST` / `FULL_PUBLISHED_ID` under staged, including a kill before the third
publish of a full run.

## Tests

`tests/enterprise_pdf_rag/object_backend/test_document_db.py` (one file; scopes never read each
other; cross-scope dedup; the PDF inline and byte-identical after a restore; explicit external
types win; kill before rename; local-disk loss; crashed local progress; foreign publisher;
concurrent writers and nested cross-scope transactions; `layout_mismatch` both ways; default
modes unchanged). The ADR 0036 conformance pack runs on a fourth backend, `document` (a
`StagedDocument` scope). `test_staged_store_wiring.py` pins one file per document;
`test_staged_model_cache_concurrency.py` reruns the ADR 0042 / 0045 cases (single flight, budget,
429 cooldown, interrupt, documents and pages at once) on the document db. The test helpers
(`store_digest`, `_logical_tree`, `model_cache_helpers`) expand `document.sqlite` into the same
logical files, so every pinned digest is unchanged.

## Weaker / unverified

- Nothing here is measured on Databricks.
- Every boundary republishes the **whole** document db, including when only the model cache
  changed. For a 300-page full-model document ADR 0046 estimated 150–250 MB for
  `processing/store.sqlite`; adding the source store (compressed page SVGs, inline PNGs) and the
  PDF puts one file around 200–350 MB — still under the 500 MB Workspace file cap, but
  `APP_OBJECT_STORE_MAX_DB_BYTES` (400 MiB) now measures the combined db, so object-heavy decks
  externalize objects > 16 KiB sooner (more files, never a db over the cap).
- A document written by ADR 0044 / 0046 (or by `sqlite`) must be re-ingested under staged;
  its model cache is not carried over, so its model calls are made again.
- Readers in another process must use `staged` too (point 5); an API on `auto` refuses the
  document instead of serving it.
- Resolution is by name: any staged store root named `source` / `processing` becomes a scope of
  its parent's `document.sqlite`.
- Cross-scope dedup is mechanical; this deck shares no digest between source and processing.

## Rejected alternatives

- **A `scope` column** in the existing tables: changes primary keys and every query of the
  default backend.
- **`ATTACH` three dbs**: still three files.
- **One core per scope on the same file**: a write in one scope inside another scope's
  transaction (same thread, second connection) waits on its own write lock until the busy timeout.
- **Import the four-file layout on first open**: more code, and it must delete files on the mount.
- **Materializing the PDF to a temporary path**: no consumer needs one (point 4).
