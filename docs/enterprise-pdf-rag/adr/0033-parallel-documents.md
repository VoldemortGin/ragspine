# ADR 0033: run-folder ingests several PDFs at once

Status: Accepted, 2026-10-06. Amends `run_folder_pipeline` (`adapters/folder_pipeline.py`), its
shared budget ([ADR 0022](0022-run-folder-question-docs-budget-and-progress.md)) and the
in-process sampling-refusal memory ([ADR 0021](0021-sampling-parameter-fallback.md)). Nothing on
disk changes: no file name, layout, fingerprint, cache key or artifact byte differs, and an
ingestion directory written before this ADR is reused as is. With `max_parallel_documents=1`
(the default) the pipeline runs exactly as before, byte for byte.

## Context

`run_folder_pipeline` was serial end to end: documents one after another, pages one after
another, one model call at a time. On Databricks the store sits on Workspace files (FUSE: every
file operation is a network round trip) and every model call goes to Azure OpenAI (seconds per
call), so almost all of a folder run is spent waiting on the network, and a folder of large
reports takes hours. Documents are independent by construction — each lives in its own
`<root>/<sha256>/` with its own source and processing stores, stage cache and model cache — so
running several at once overlaps that waiting without touching what any one document does.

## Decision

### 1. `max_parallel_documents: int = 1`, threads, one document per thread

`run_folder_pipeline(max_parallel_documents=N)` (CLI `run-folder --max-parallel-documents N`;
1..`MAX_PARALLEL_DOCUMENTS` = 16, anything else is a `ValueError` before any work) discovers,
hashes and de-duplicates every PDF in the main thread exactly as before, then runs
`_run_document` for the remaining ones on a `ThreadPoolExecutor` of `min(N, documents)` workers
named `run-folder-*`. N = 1 takes the old loop and never starts a thread.

Threads, not processes: the work is I/O-bound, and a process pool would split exactly what has
to stay shared — the budget total, the sampling-refusal memory, the progress callback and the
embedder — into copies that can no longer be counted or bounded.

Each worker builds everything a document touches itself, as the serial loop always did:
`ingest_pdf` opens its own `LocalDocumentStore` / `ProcessingStore` / `JsonCompletionClient`
and its own pdfspine `Document`s (inside its own `shared_pdfs()` scopes); `_run_document`
opens its own stores and tree client; `recording_repairs()` is entered inside the worker. Two
workers never handle the same PDF (de-duplication by sha256 happens before any job starts), so
no `.claim`, stage-cache entry, `current-*` pointer or object is ever written by two threads.

Worker threads start with an **empty** `contextvars` context (the executor does not copy the
caller's). That is deliberate: a caller's `shared_pdfs()` scope would otherwise hand one
opened pdfspine `Document` to several threads, and a caller's `recording_repairs()` counters
would be incremented from several threads. Each document's own scopes are opened inside its
worker; `DocumentRun.storage_repairs` is that document's, as before.

### 2. Thread-safety review of everything a document shares

| State | Where | Verdict |
|---|---|---|
| Per-document stores, stage cache, model cache, `.claim`s | `<root>/<sha>/…` | Never shared: one worker per sha. ADR 0020's non-atomic first-writer-wins fallback only matters between writers of the same file, which cannot happen across documents. |
| Store verification memory (ADR 0024) | per store **instance** | Instances are created inside the worker; nothing crosses threads. |
| pdfspine `Document` | `open_pdf` / `shared_pdfs()` | One per thread (the scope is a context variable and workers start empty); never shared. |
| `_Budget` (shared total) | `folder_pipeline` | Was unsynchronised read-then-write. Now locked, and allotments are **reserved** (§3). |
| Sampling-refusal registry `_UNSUPPORTED` | `json_completion` | Already locked. First calls are now gated while documents run at once (§4). |
| `JsonCompletionClient` | per document / per stage | Not shared between documents; its own lock already serialises its calls. |
| Embedder (`LocalEmbeddingAdapter` or injected) | one object for the run | `request_count` / batch-failure counters are plain `+=`, and `processing_retrieval` reads `request_count` before and after a batch, so a concurrent document's requests would land in another's `DraftIndex.embedding_requests`. Each document now gets a `_DocumentEmbedder` view: calls go through one lock, and its `request_count` counts its own requests (§5). |
| Progress callback | caller's | Entered under one lock (§6). |
| `get_settings()` (`lru_cache`), `source_paint*` `lru_cache`s, `pdfspine_tsr._digest` `cache` | module level | `functools` caches are thread-safe; at worst a value is computed twice. The TSR / ONNX layout model objects are pdfspine's; their thread safety is pdfspine's and is not exercised by the offline suite (risk below). |
| `<root>/model-cache`, `answers-audit.sqlite`, `scan_catalog` | answer stage | Only touched after every worker has finished (§8). |

### 3. The shared total is reserved, so it is never overspent

`_Budget.allot(wanted)` grants `min(wanted, total − used − reserved)` under a lock and
**reserves** it; `spend(calls, granted)` counts the calls actually made and releases the
reservation. A document's ingest allotment is settled after `ingest_pdf`, its tree allotment
after the tree, the answer allotment after the evaluation, and whatever a failed or stopped
stage never settled is released (uncounted, as the serial run never counted it) in
`_run_document`'s `finally`. Because each client never makes more live calls than it was
granted, the total is never exceeded however the documents interleave.

One at a time, every reservation is settled before the next allotment, so each allotment sees
exactly `total − used`, as before; the ADR 0022 tests pass unchanged. At once, a document can be
cut (`budget_starved`, `budget_exhausted=true`) because others hold reservations they may not
use up — which document is cut then depends on timing, never on more calls than the total.
`max_live_calls_per_pdf` (an integer or `"auto"`) stays per document.

### 4. One sampling probe for clients started at once

`json_completion.one_sampling_probe()` is a process-wide scope (a counter, so worker threads see
it) that `run_folder_pipeline` holds while documents run at once. Inside it, the first call to
an (endpoint, model) is sent alone: a concurrent first call from another thread waits on a
per-endpoint lock until it has finished — whatever its outcome — and then sends what it
learned, so a refused `temperature` costs one 400 per run, not one per document, and each
document's model cache holds the same records the serial run writes (the
`sampling_parameter_unsupported` skip record, not a 400 of its own). Once one call has finished
nobody waits again.

Outside any scope nothing waits, exactly as before. The gate is not global because it would
serialise unrelated first calls — the `document-catalog` service's concurrent requests, or two
clients racing for one claim (ADR 0023), whose sender may legitimately wait on the other.
A sender that waits on another thread's call must not run inside the scope.

### 5. Embedding calls go one at a time

The per-document `_DocumentEmbedder` (or `_DocumentBatchEmbedder` for a `BatchEmbeddingPort`,
so batching is unchanged) holds one shared lock around each embedding request. Embedding is a
small share of a document's time (one batch of up to 16 index texts per request, against two or
more model calls per page), so this costs little; in exchange the counts are exact and the
shared adapter's state (`_arrays_refused`, batch failures) is learned once for the run, as in
the serial run. Injected embedders get the same view.

### 6. Progress: interleaved per document, entered one thread at a time

`discovered`, `question_docs_resolved`, `document_skipped` and `done` come from the main thread
as before. `document_start` / `document_progress` / `document_done` of different documents now
interleave; every one of them already names its `pdf`, and in parallel mode each also carries
`slot` (1..N, the worker it ran on), so `print(event, payload)` in the notebook stays a
readable, one-line-per-event log. A document's own events stay in order (start, its pages, its
stages, done). The `document_progress` throttle (each stage's first and last page, at most one
line per 10 pages or 30 s) is a closure per document, so it counts per document. The callback is
entered under one lock, so it never runs on two threads at once and lines never tear. With
N = 1 no event carries `slot`.

### 7. Results, failures and stopping

`FolderPipelineResult.documents` keeps discovery order (each job's place is fixed before it
starts), not completion order. `live_calls`, `tree`, `sampling_parameters_dropped` and
`storage_repairs` are summed from the per-document results after every worker has finished;
`report.json` / `report.md` are written as before.

A failing document does not affect the others. In parallel mode a worker records **any**
`Exception` (not only `ValueError` / `OSError`) as that document's `failed`, with its stage and
`"<Type>: <message>"` as `error`, because an exception escaping one worker must not abandon the
documents running beside it. With `continue_on_error=False` the first failure seen is raised —
after the others have stopped as below.

Stopping. `ThreadPoolExecutor` cannot interrupt a running thread, and a plain shutdown (or
leaving its `with` block) still runs every *queued* document to the end — an interrupted
notebook would keep spending money in the background. So when anything stops the run (a
`KeyboardInterrupt` in the waiting main thread, or a failure under `continue_on_error=False`):

1. a shared `cancel` event is set and a `stopping` event (`reason`, `running`) is emitted;
2. queued documents are cancelled (`shutdown(cancel_futures=True)`) and never start;
3. running documents stop at their **next page boundary** (the ingest page reporter, which is
   always installed in parallel mode, raises `_Cancelled`) or stage boundary — never inside a
   model call: the page's calls in flight finish, their records are written and their claims
   released, so a rerun replays them instead of paying for them again, and no orphaned `.claim`
   is left behind;
4. the main thread waits for them, then re-raises.

The main thread waits in 0.2 s polls, so an interrupt is seen at once (a blocking wait on a
lock is not interruptible on every platform). `_Cancelled` is a `BaseException`, so no stage's
`except Exception` mistakes it for that stage's failure. A second interrupt during the wait
stops the waiting, not the workers: they still stop at their next page. The claim takeover of
ADR 0023 remains the backstop for a process that is killed outright.

### 8. Not parallel: answering the questions

The evaluation stays one question at a time. It would gain little — the one answer client holds
its lock for the whole network call, so concurrent questions through it are serial anyway — and
running it at once needs answers this ADR does not have: per-thread answer clients would split
the answer budget and share `<root>/model-cache` (claims are `O_EXCL`, so that part is safe);
`answers-audit.sqlite` opens a connection per write in WAL mode, and concurrent writers would
contend on its one write lock with sqlite's default busy timeout; and the thread safety of the
mounted catalog's in-memory hydration caches under concurrent requests has not been reviewed.
No `max_parallel_questions` is added.

## Recommended setting

`MAX_PARALLEL_DOCUMENTS = 4` for the Databricks notebook, raised only after a clean run:

- **Azure OpenAI limits.** Each worker keeps at most one call in flight (layout vision calls
  carry a page PNG and up to 2 048 output tokens), so N workers are at most N concurrent
  requests. Four stay well inside a typical deployment's requests-per-minute and
  tokens-per-minute quota. This matters more than it seems: a 429 is not treated as transient —
  like every failure it is recorded and replayed (ADR 0021's permanent failure record), so a
  page or object rate-limited once stays failed until its record is removed. Concurrency raises
  that probability; keep N where the deployment never answers 429.
  **Resolved by [ADR 0034](0034-transient-provider-errors.md) (2026-10-06):** 429 / 408 / 5xx /
  timeouts / connection errors are now transient — retried within the call with jittered
  backoff, a `Retry-After` shared by every worker calling the same (endpoint, model), and never
  recorded, so a rerun calls them again (old such records included). Lower N when the status
  table's `retries` / `transient_failures` are frequent.
- **Workspace files (FUSE).** Every file operation is a round trip; four documents overlap those
  waits without driving the workspace API hard. Each document still writes its own directory.
- **Driver memory.** A worker holds its PDF's bytes (read whole, often tens of MB for an annual
  report), the opened pdfspine document, and one rendered page PNG at a time (a few MB); four
  workers stay within a few hundred MB on a standard driver.
- **Measured.** A scripted endpoint with 0.5 s per call, eight 3-page PDFs (48 calls): one at a
  time 27.3 s, two 14.3 s (1.9×), four 7.8 s (3.5×). The offline test's CPU-bound pdfspine work
  does not overlap (one interpreter lock), which is why four is not 4×; against real latencies
  of several seconds per call the waiting dominates and the gain approaches N.

Raising N beyond the endpoint's comfortable concurrency buys nothing and risks permanent 429
records (since ADR 0034: retries, waiting and pages left for the next run); lowering it to 1
restores the old run exactly.

## Consequences

- With N = 1 nothing changes; `FULL_STORE_DIGEST` and every ADR 0022 budget test still hold.
- With N > 1 every document writes the same bytes, processing ids, pointers and model-cache
  records as the serial run (pinned against a serial run, with and without hard links), only
  sooner; the order of `document_*` events changes, the order of `documents` does not.
- A run under a tight `max_live_calls_total` may starve a different document than the serial
  run would (§3); it never spends more than the total. As before, the calls of an ingest that
  raised are not counted against the total (pre-existing; neither mode can see them).
- Tests: `tests/enterprise_pdf_rag/adapters/test_parallel_documents.py`.

## Rejected alternatives

- **A process pool.** Splits the budget, the refusal memory and the progress log; a total could
  no longer be bounded without a cross-process ledger.
- **Parallel pages or objects inside one document.** Larger change to the ingest pipeline's
  ordering, stage cache and claim semantics; documents are already independent.
- **A global first-call gate.** Serialises unrelated callers (§4).
- **Copying the caller's context into each worker.** Would share one caller-scope pdfspine
  `Document` and one repairs counter across threads.
