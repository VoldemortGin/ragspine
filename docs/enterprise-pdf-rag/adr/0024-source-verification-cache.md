# ADR 0024: A store instance verifies a content-addressed snapshot once

Status: Accepted, 2026-10-05. Amends the read path of the local
stores behind [ADR 0001](0001-architecture.md) (content-addressed immutable snapshots) and
[ADR 0010](0010-generic-pdf-ingestion-entry.md) (the ingestion directory), and complements
[ADR 0020](0020-storage-without-hard-links.md) (stores without hard links). It changes **no
on-disk layout, no file name, no file content, no request fingerprint and no content id**: an
ingestion directory written before this ADR is read, reused and resumed exactly as before.

## Context

A user's `run_folder_pipeline()` on Databricks (project root on Workspace files, a FUSE mount
where every file operation is a network round trip and `os.link` fails, so ADR 0020's fallback
re-reads every placed file) ran for more than eight hours on encrypted reports of a few hundred
pages without finishing.

`LocalDocumentStore.load(manifest_id)` re-read and re-hashed the manifest, the whole PDF and
every page's SVG and text — 2P+2 files — on **every** call, and the pipeline called it about
P + 5M + T + 12 times per document per round (P pages, M indexable objects, T tables /
formulas): once per page in the page-metadata stage, once per object when indexing, once per
object in each `save_draft` that carries a retrieval plan (index and publish), once per object
in the catalog scan and again in the mount, and once more per table / formula proof, which also
re-read and re-decrypted the whole PDF. `ProcessingStore.save_draft` / `load` likewise
re-verified every processing asset on each call. Reads therefore grew as O(P·M), and bytes read
as O(P·M·page size): on a 30-page synthetic document with 8 text objects a page (240 KB PDF),
one round read files 141 158 times and 2.07 GB, of which one `save_draft` read 783 MB and the
scan plus mount before answering 725 MB.

## Decision

1. **Per-instance verification cache in `LocalDocumentStore`.** An instance keeps
   - `digest → byte length` for every object **it has itself read back and hashed** (`get`,
     `read_content`, `verify`, the existing-object branch of `put`). An object it only wrote is
     not in the set, so the first sweep after a write still reads it from disk once;
   - `manifest id → DocumentSnapshot` for every source manifest it has fully verified.

   Keys are content identities only — the SHA-256 that names the file and the length the
   reference states — never a path, size or mtime. The manifest id is the digest of the
   manifest bytes, and every asset it names is named by digest, so a verified snapshot is
   exactly that snapshot for as long as the instance lives.
2. **Only verification-only sweeps use it.** `verify(ref)` (new), `load`, `publish`, `put` of
   an object already on disk, `ProcessingStore.save_draft` / `load` / `cache` / `_read_retrieval`
   asset sweeps and `save_document_tree` skip a digest the same instance already verified.
   **Bytes a caller consumes are always re-read and re-hashed**: `get`, `read_content`, the
   page text sidecars, SVGs, IRs and receipts every proof reads, `ProcessingStore.cached`
   (which must notice a missing or changed stage output), and the processing manifest object on
   every `ProcessingStore.load` — the one file that names all the rest, which the mount's drift
   guard relies on. `ProcessingStore.load` results are deliberately **not** cached.
3. **Scope is one instance, so one stage.** Nothing is module-level or shared between instances.
   Every pipeline stage (`ingest_pdf`, `qualify_draft`, `index_draft`, `publish_draft`), every
   catalog scan entry and every mount constructs its own stores, so each of those re-checks the
   disk once — the verification that "confirms the bytes on disk now" (before a pointer moves,
   when a release is scanned, when it is mounted) is kept **once per stage instead of once per
   object**. A new process starts with empty caches.
4. **`put` of an object already on disk** reads it back and verifies it instead of first
   writing, fsyncing and trying to link a temporary — the same outcome the refused link led to
   (a corrupted existing object is still refused, never overwritten or treated as present). On
   the instance that already verified that digest it returns without I/O.
5. **One read and one open of a source PDF per scope.** `adapters/shared_pdf.py`: inside a
   `shared_pdfs()` scope `source_pdf(sources, snapshot)` reads the verified PDF bytes once and
   `opened_pdf(pdf)` opens (and authenticates, through `pdf_password.open_pdf`) identical bytes
   once, keyed by their SHA-256, for every table, formula and chart proof; the documents are
   closed when the scope ends, a nested scope defers to the outer one, and the scope is a
   context variable, so nothing crosses threads or tasks. Outside a scope every call opens and
   closes its own document exactly as before. Scopes: `validate_processing_source`,
   `ProcessingRetrieval.build`, `ProcessingPipeline.run` (ingest) and
   `requalify_visual_objects`.
6. **Loop-invariant load hoisted**: `annotate_page_metadata` loads the source snapshot once
   instead of once per page, so the stage stays linear even with the cache off.
7. **Turning it off.** `LocalDocumentStore(verify_every_load=True)` /
   `ProcessingStore(verify_every_load=True)` restore verify-on-every-call; the default (`None`)
   follows `APP_VERIFY_EVERY_REQUEST` (`Settings.verify_every_request`), the existing audit
   switch, so one setting puts back "verify everything every time" for both answering and
   ingestion. `ProcessingStore(verify_every_request=True)` implies it. `auditing()` returns the
   same store without the cache; the AIA source-review app (`create_aia_app`) always uses it,
   because every one of its requests is documented to re-verify the live bytes it serves. No
   settings field was added.

## Threat model

The invariant "a changed byte on disk is refused" now holds at these points:

- **Every consuming read**, always: a tampered object a stage actually reads raises
  `digest mismatch` at that read, cached or not.
- **Every new store instance**: the first `load` / sweep of each stage, each catalog scan entry
  and each mount re-reads and re-hashes everything it verifies.
- **Every `ProcessingStore.load`**: the pinned manifest object is re-read and re-hashed.
- With `APP_VERIFY_EVERY_REQUEST=1` / `verify_every_load=True`: every call, as before.

What is weaker: **within one store instance**, an asset another process (or a person) rewrites
*after* that instance verified it, and that the instance then only verifies again rather than
consuming, is not noticed by that instance. It is noticed by the next consuming read of that
object, by the next stage (a new instance), by the catalog scan and by the mount. An instance
lives for one stage of one document (or one mount), the store is written by one pipeline at a
time (ADR 0020 already requires that), and every byte that reaches a proof, a prompt or an
answer is still hashed at the moment it is read; the cache never lets unverified bytes through,
it only skips re-confirming bytes nobody is about to use. Whoever needs "re-check the whole
snapshot on every call" sets the audit switch.

## Measured effect

Harness: `run_folder_pipeline` over one synthetic PDF (8 text objects a page), no hard links
(`os.link` refuses with `EPERM`, ADR 0020 fallback), offline layout / metadata model,
offline embedder, the in-process scan + mount the answering step uses, counted by wrapping
`open` / `os.*` (scratchpad `agentA/prof.py`, derived from the investigation's `perf/prof2.py`).
"Rerun" = the same command again with every model output cached; the budget-limited run gives
each round 30 live calls.

| pages | run | file reads before → after | MB read before → after | fsync before → after | wall s before → after |
|---|---|---|---|---|---|
| 30 | first | 141 158 → 32 818 | 2 067 → 233 | 4 964 → 4 962 | 17.6 → 12.8 |
| 30 | rerun | 97 794 → 21 095 | 1 351 → 157 | 1 205 → 2 | 10.0 → 7.1 |
| 40 | first | 220 958 → 43 728 | 3 461 → 312 | 6 614 → 6 612 | 23.5 → 16.2 |
| 40 | rerun | 150 354 → 28 105 | 2 232 → 210 | 1 605 → 2 | 14.5 → 8.7 |
| 60 | first | 429 758 → 65 548 | 7 305 → 469 | 9 914 → 9 912 | 43.2 → 25.1 |
| 60 | rerun | 285 474 → 42 125 | 4 637 → 316 | 2 405 → 2 | 24.7 → 13.3 |
| 40, budget 30 | round 1 / 2 / 3 | 166 218 / 222 443 / 223 323 → 32 378 / 39 127 / 38 278 | 2 622 / 3 491 / 3 499 → 238 / 299 / 295 | 4 774 / 3 368 / 2 248 → 4 772 / 2 166 / 646 | 18.2 / 22.2 / 21.8 → 11.8 / 16.5 / 15.0 |

Growth from 30 to 60 pages: reads ×3.04 before (exponent ≈ 1.6, bytes ×3.5) and ×2.00 after;
all file operations together (open / os.open / fsync / replace / link / stat / mkdir / unlink)
grow with exponent 1.47 before and 1.00 after. Same published processing ids before and after
in every row; a pre-change ingestion directory reruns under the new code with zero live calls
and no object or stage-cache entry added, changed or removed.

With 5 ms injected into every `open`, `os.open`, `fsync`, `replace` and `link` (a crude model
of a network filesystem; stat / mkdir / unlink not delayed), 40 pages: first run 1 723 s →
533 s, rerun 1 065 s → 201 s. Extrapolated (a linear fit after, a quadratic fit before, all
file operations counted, 20–50 ms each, **not measured on Databricks**), a 300-page document
of this density would need about 0.86 M file operations on a first run (≈ 5–12 h) and
0.26 M on a rerun (≈ 1.5–3.5 h), against about 8.6 M (≈ 48–120 h) and 5.2 M (≈ 29–72 h)
before.

What remains per round (all linear): writing every new content-addressed object (temporary,
fsync, refused link, existence check, rename, read-back — ADR 0020), the object stage
re-deriving and re-checking each stage output on a rerun (about 20 reads an object), one full
verification per stage / scan / mount, `export_review` (source PDF, `text.json`, one HTML per
page) and `export_processing_review` (about 3P + 7M files, for ingest and again for index),
which are rewritten every round, and the layout PNG, which is rendered even when the budget is
spent because the request fingerprint (and so the cache lookup) includes the image bytes.

## Rejected alternatives

- **A process-wide cache keyed by store root.** Would make "a new instance verifies again" false
  and let a long-lived mount skip the check a fresh scan should make.
- **Caching `ProcessingStore.load` results.** The mount's drift guard falls through to `load`
  precisely to refuse a rewritten manifest object; a cached result would accept it.
- **Keying on size + mtime.** A rewrite can keep both; only the digest is an identity.
- **Skipping the review exports when they exist.** Their pages are derived from immutable
  inputs, but `review.html` also folds in mutable run records, and a code change that alters
  the HTML would leave stale pages behind; the layout has no marker that says which code wrote
  them. Left for a separate decision.
- **Deferring the layout PNG until the budget allows a call.** A cached reply is found by a
  fingerprint over the request body, which embeds the image; skipping the render would skip
  replay.

## Amendment 1 (2026-10-06): the full verification persists as a receipt (ADR 0034)

Decision 3 ("a new process starts with empty caches", "each of those re-checks the disk once")
is narrowed by [ADR 0034](0034-persisted-verification-receipts.md): after an instance that did
not write a snapshot has statted, read and hashed all of it, it records a receipt beside the
objects; a later instance (another stage, scan, mount or process) whose receipt is intact,
for the same manifest and file set, with every not-yet-read file's size / mtime / ctime
unchanged, replaces the sweep of `LocalDocumentStore.load` and `ProcessingStore.load` with one
`stat` per object. The final `publish_draft`, `save_draft`'s own sweep, `publish`, `verify`,
`put` and every consuming read still read for real; the scan and the mount reuse receipts but
never write one. `APP_VERIFY_PERSISTED_RECEIPTS=false` restores this ADR exactly;
`APP_VERIFY_EVERY_REQUEST=1` still verifies every call. The threat-model paragraph above now
also applies across instances until a file's stat moves — see ADR 0034's threat model.

## Amendment 2 (2026-10-09): the layout PNG is skipped when nothing can be replayed (ADR 0039)

The last rejected alternative ("deferring the layout PNG until the budget allows a call") still
holds whenever the model cache holds any record. [ADR 0039](0039-skip-the-layout-png-when-no-call-can-be-made.md)
skips the render only when no live call is left **and** the cache holds no record at all, so no
cached reply can be missed; the page then fails with the same `call_budget_exhausted` the client
would have raised.
