# ADR 0045: Pages and objects of one document at once

Status: Accepted, 2026-10-09. Amends `ProcessingPipeline.run` (`adapters/aia_processing.py`), the
generic ingest that builds it (`pdf_ingestion._ingest_pdf`), the layout page image
(`page_partition.ModelPagePartitioner`) and the "parallel pages or objects inside one document"
rejection of [ADR 0033](0033-parallel-documents.md). Builds on
[ADR 0042](0042-ingest-concurrency.md) (a client's lock no longer spans the network). With
`APP_PAGE_CONCURRENCY=1` (the default) the page loop runs exactly as before, byte for byte.

## Context

Measured on Databricks, after embedding the second largest share of an ingest is `layout_vlm`:
`ProcessingPipeline.run` walks the selected pages one after another and every fallback page sends
one vision call (a 960 px PNG plus the span JSON, seconds to tens of seconds each; a 300-page
report has around 60 such pages). Semantic objects are the same: `SemanticObjectAdapter.process`
is called once per object, in order, and every chart / formula / diagram / image costs one or two
model calls. ADR 0033 overlaps the waiting of different documents, but a folder with one large
report still waits page after page.

ADR 0033 rejected parallel pages and objects as a "larger change to the ingest pipeline's
ordering, stage cache and claim semantics". Each of the three is now answered without changing
any of them:

- **Ordering.** Work runs at once, results do not: pages are taken in page order and every
  object record is put back in its object's place, so the manifest — and with it every stage
  fingerprint that names an earlier output, the processing id and `pages_*` counts — is the
  serial manifest. Nothing in the pipeline depends on *when* a page was computed, only on its
  inputs (every stage fingerprint is a hash of inputs, never of position or time).
- **Stage cache.** Entries are content-addressed and immutable; every page and object writes
  its own fingerprints, so no two threads write the same entry except for byte-identical
  content (both backends then keep the first writer and accept the second: `link_new_file` /
  `INSERT OR IGNORE`, ADR 0020 / 0036).
- **Claims and the budget.** Since ADR 0042 one `JsonCompletionClient` serves several threads:
  the budget is spent atomically (`_take_call`), one request fingerprint is in flight once
  (single flight), claims are per request (ADR 0023) and the `Retry-After` cooldown is shared
  process-wide per (URL, model) (ADR 0035). Before ADR 0042 the client's lock held every call
  for its whole round trip, so pages at once would have gained nothing.

## Decision

### 1. `APP_PAGE_CONCURRENCY` (`Settings.page_concurrency`), default 1, 1..16

`ProcessingPipeline(page_concurrency=N)` (anything outside 1..16 is a `ValueError` at
construction); the generic ingest passes `get_settings().page_concurrency`. N = 1 takes the old
loop untouched (one store transaction per page, ADR 0036 §7.2). The AIA review runtime
(`processing_runtime.py`) keeps the default.

### 2. N > 1: two pools, one semaphore

- A page pool of N threads (`ingest-page-*`) runs whole pages: page input, canonical stage,
  partition, normalisation, then the page's objects.
- An object pool of N threads (`ingest-object-*`) runs `ObjectProcessor.process` per object; a
  page thread submits its objects there and waits for them in object order.
- One `BoundedSemaphore(N)` is held around each unit of model-bearing work — a page's
  `partitioner.partition` call and an object's `process` — and never while waiting. So pages
  and objects of a document together have at most N units in flight, a page thread waiting for
  its objects holds no permit (no nested-pool deadlock), and a document has at most 2N + 1
  threads. Without an object processor (layout-only stages) objects stay inline.
- All page futures are submitted at once (FIFO, so calls start in roughly page order). The
  calling thread takes them in page order and calls `on_page(done, total)` after each, so
  `document_progress` events keep their order and their thread.
- Workers run in a `contextvars.copy_context()` of the run, so they share its `shared_pdfs()`
  scope (one read and one open of the source PDF for every page and object) and its
  `recording_repairs()` counter. This is the context ADR 0033 refused to copy into *document*
  workers: there it would have shared one caller's `Document` between documents; here it is the
  document's own scope, shared by its own pages.
- A thread waiting on a future polls every 0.2 s, so an interrupt reaches a waiting main
  thread on every platform (ADR 0033 §7).

### 3. No page transaction when pages run at once

A worker must not hold the store's write transaction across a model call: on sqlite that is
`BEGIN IMMEDIATE` on the store db (the process-scoped writer), and every other page's write
would wait on it up to the busy timeout. So with N > 1 there is no per-page transaction; each
write commits on its own, as every write did before ADR 0036 §7.2. Every entry is still
immutable and content-addressed, so a crash loses at most the entries not yet written and their
model calls replay from the model cache. The cost is more commits (each an fsync with
`synchronous=FULL`) — on a FUSE mount more round trips per page; measure `store_write` before and
after raising N. The source store, the model cache (its own db, per-transaction lease) and the
publish at the end are unchanged.

### 4. Shared state, reviewed

| State | Verdict |
|---|---|
| `ProcessingStore` / `LocalDocumentStore` instance memory (`_verified`, `_written`, `_damaged_seen`) | Dict / set single operations; at worst a digest is verified twice. |
| sqlite backend | One connection per thread (ADR 0036), `_txn_depth` is a context variable (each worker starts at depth 0). **Fixed here:** `_acquire_writer` checked "already acquired?" outside its lock, so page threads making a store's first writes together counted the process writer lease several times and `close()` never released it — every other process would then get `store_busy` until this one exited. The check is now inside the lock. |
| files backend | Temporary file + `link_new_file`; a racing writer of the same object or stage entry finds an intact copy and returns `existing`. |
| Model cache / `JsonCompletionClient` | ADR 0042: counters, budget and in-flight table under the client lock, network outside it; single flight per fingerprint; claims per request. |
| `shared_pdfs()` scope | Shared on purpose (§2). Its get-or-read / get-or-open now runs under a lock in the scope, so a PDF is read and opened once, not once per racing thread. pdfspine `Document` / `Page` methods take `&self` and are shared across threads. |
| `TextPageRouterPartitioner._stats` | Lazily scanned document statistics; now computed under a lock, once, instead of by every page thread that starts first. |
| ONNX layout (`onnx_partition.py`) | Inference is inside pdfspine (`Page.find_layout`, sessions cached per model there); this code keeps no shared numpy buffer or other mutable state between pages. Not exercised by the offline suite (`onnx` marker). |
| `SemanticObjectAdapter.skipped_calls`, `recording_repairs()` counters | `Counter[k] += n` from several threads; under the interpreter lock only the first insertion of a key runs Python code (`__missing__`) and could interleave. A lost increment would undercount a report line, never change an artifact. Left as is (`semantic_objects.py` is out of scope here). |
| Progress callback | Called only from the run's own thread, in page order. |

### 5. Budget

A tight `max_live_calls` is never exceeded: each client spends a unit atomically before it
sends. Which pages get the last units can differ from the serial run (pages start in order but
finish in any order, and a retried call spends a unit), exactly as ADR 0033 §3 describes for
documents. Pinned: 8 pages, budget 3, N = 4 → three requests sent, `live_call_count == 3`.

### 6. Stopping

When `on_page` raises (the run-folder `_Cancelled` at a page boundary, a `KeyboardInterrupt`, a
failing page's exception reaching its turn) the page pool is shut down with
`cancel_futures=True` — queued pages never start — and waited for, then the object pool: pages
already running finish their calls, records and claims, so a rerun replays them and no `.claim`
is left. At most N pages run past the stop.

### 7. Documents × pages, and 429s

Requests in flight are at most `MAX_PARALLEL_DOCUMENTS × APP_PAGE_CONCURRENCY` (plus one
embedding semaphore, ADR 0042). The ADR 0035 cooldown is keyed by (URL, model), process-wide,
so every page thread of every document honours the same `Retry-After`: pinned with 2 documents ×
4 pages, where every attempt after a 429 waits out its `Retry-After` first.

### 8. `APP_LAYOUT_PNG_WIDTH` (`Settings.layout_png_width`), default 960, 64..4096

The fallback-page render width is configurable. At 960 nothing changes. Any other width is
appended to the partitioner fingerprint (`…:png-width=<w>`): the PNG bytes already change the
model-cache request fingerprint, but the partition *stage* is keyed by the partitioner
fingerprint, so without the suffix a changed width would silently reuse every cached 960 px
partition. Changing the width therefore invalidates the cached layout of every model-layout page
(new stage entries and new calls on the next run), which is what changing it is for. Larger
images cost more tokens and time per call.

## Recommended setting

Documents 4 × pages 4 (`MAX_PARALLEL_DOCUMENTS = 4`, `APP_PAGE_CONCURRENCY=4`): at most 16 model
requests in flight. Start there only after a clean run at 4 × 1, and lower N when the status
table's `retries` / `transient_failures` climb; a deployment's requests-per-minute and
tokens-per-minute quota, not this code, is the limit. Memory: each in-flight layout call holds
one page PNG (a few MB), so 16 stay within a standard driver. For a single large report
(`MAX_PARALLEL_DOCUMENTS = 1`), `APP_PAGE_CONCURRENCY=8` gives the same overlap one document
can use.

## Measured

Offline, a scripted endpoint answering each layout call after 0.5 s, a 12-page authored PDF,
`stage="layout"`: N = 1 6.38 s, N = 2 3.28 s (1.9×), N = 4 1.84 s (3.5×), N = 8 1.28 s (5.0×),
the same processing id each time. The 71-page sample presentation with `max_live_calls=0`,
full and lite: the same processing id at N = 1 and N = 4 (and the same as before this ADR).

## Consequences

- N = 1: nothing changes; `FULL_STORE_DIGEST` and every existing test hold.
- N > 1: the same stage entries, manifest and processing id as the serial run (pinned against
  `FULL_STORE_DIGEST` on both object backends, and against a serial run with documents and
  pages at once); no per-page transaction (§3); up to 2N + 1 threads per document.
- Page metadata (`annotate_page_metadata`), the document tree and answering stay serial.
- Traces and records carry codes, counts and timings only, as before.
- Tests: `tests/enterprise_pdf_rag/adapters/test_page_concurrency.py`.

## Rejected alternatives

- **One pool for pages and objects.** A page waiting on its objects would hold a worker the
  objects need; with N workers and N pages waiting, nothing runs.
- **Nested pools per page.** Threads grow with pages × objects and the in-flight total is no
  longer bounded by N.
- **Keeping the page transaction and running only the model calls in workers.** Object
  processing interleaves its writes with its calls inside `semantic_objects.py`, which this
  change does not restructure.
- **Overlapping the page metadata stage too.** It runs after the page pipeline as its own pass
  with its own transaction per page; left for a later change once these numbers are in.
