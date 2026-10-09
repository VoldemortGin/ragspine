# ADR 0039: Model calls of one client overlap, embeddings share a semaphore, HTTP keeps alive

Status: Accepted, 2026-10-09. Amends `JsonCompletionClient` (its lock), §2 / §5 of
[ADR 0033](0033-parallel-documents.md) (the client "serialises its calls"; "embedding calls go
one at a time") and the transports of `providers._send_once` / `local_models._send_local_once`.
Nothing on disk changes: no fingerprint, record, response, context, cache key or artifact byte
differs. The default `max_parallel_documents` stays 1.

## Context

ADR 0033 overlaps the waiting of different documents, but inside each document three things
still waited in line:

- `JsonCompletionClient` held one lock around the whole `_call` — cache lookups, the claim, the
  network round trip and every 429 backoff — so any caller that shares one client across
  threads got one request at a time.
- `run_folder_pipeline` put one global `Lock` around every embedding call of every document,
  so with N documents running at once the embedding share of the run did not shrink at all.
- Every model and embedding request opened a fresh TCP + TLS connection and closed it after the
  reply: one extra handshake (often 100–300 ms to a cloud endpoint) per request.

## Decision

### 1. The client lock guards state, not the network

`JsonCompletionClient._lock` is now held only to change shared state: the counters
(`cache_hit_count`, `retry_count`, …, through `_tally`), the dropped-parameter set, the live-call
budget and the in-flight table below. No lock is held while sending, pausing for a backoff or
reading / writing the model cache; the backends are already safe across threads (the sqlite
core opens a connection per thread and serialises its transactions; file writes are
create-if-absent, ADR 0020).

- **Budget.** `_take_call()` spends one call atomically (check and decrement under the lock).
  The early `remaining == 0` check before claiming stays; a call that loses the last unit to a
  concurrent one between that check and its send releases its claim and raises
  `call_budget_exhausted`, as if it had seen 0. A retry takes its unit before pausing and gives
  it back if renewing its claim fails (nothing sent), so `live_call_count` stays "attempts
  issued" exactly.
- **One fingerprint in flight once (single-flight).** `_single_flight(fingerprint)` is a lock per
  request fingerprint (reference-counted, dropped when idle). A second caller of a fingerprint
  in flight waits for it and then finds its record: a replay (`cache_hit`), never a second send
  and never a `request_in_progress_or_uncertain` against its own process's claim. Different
  fingerprints never wait for each other.
- **Backoff stays shared.** The 429 / `Retry-After` cooldown was already process-wide per
  (URL, model) under its own lock (`transient._COOLDOWNS`, ADR 0035); since the client lock no
  longer serialises callers, every concurrent caller now reaches `transient.pause` on its own
  and honours the same window. The ADR 0033 sampling probe is unchanged; it is taken before the
  flight lock, so the two never wait on each other in opposite orders.

### 2. Embeddings: a semaphore for embedders that count per thread

`_embedding_gate(embedder, limit)` decides what documents running at once share around
embedding calls:

- an embedder with `thread_counts()` (the production `LocalEmbeddingAdapter`) gets a
  `BoundedSemaphore(APP_EMBEDDING_MAX_CONCURRENCY)` (`Settings.embedding_max_concurrency`,
  default 4, ≥ 1);
- any other embedder (injected, unknown thread safety, shared counters only) keeps the one
  lock of ADR 0033 §5.

`LocalEmbeddingAdapter` changes its counters and what batching learned (`_batch_failures`,
`_arrays_refused`) under its own lock, and also counts each thread's requests / retries /
transient failures in a `threading.local`. `_DocumentEmbedder` measures a call by
`thread_counts()` before and after, so each document's `DraftIndex.embedding_requests` stays
exactly its own even while other documents' requests go to the same adapter. ADR 0026's
degradation is untouched: a failed batch is still halved, arrays refused on a 400 pair before
any array worked, and batching stops for the whole adapter after
`EMBEDDING_BATCH_MAX_FAILURES` failed batches — now counted across all threads.

### 3. HTTP keep-alive: one idle connection per thread and host

Both transports use `http.client` (no `requests` / `httpx` in this path). They now take a
connection from a per-thread pool (`threading.local`, keyed by connection class and
`host:port`) and put it back only when the reply was read to its end and the server did not
ask to close (`response.isclosed()` and not `will_close`); an error, an unread or partly read
body (every non-200 except a small 400 body) or `Connection: close` closes it as before. Before
reuse an idle socket that has become readable (the server closed it after its keep-alive
timeout) is discarded and a fresh connection opened, so an idle-closed connection costs no
error and no retry. The per-call `timeout` is applied to the reused socket; retries, the
transient classification and every byte sent are unchanged. A thread's pool is closed when the
thread ends (`weakref.finalize`), `forget_connections()` closes the calling thread's.

Why per-thread and `http.client`: it is the smallest change that is thread-safe without a lock
(no connection is ever shared between threads), keeps the module import-clean with no new
dependency, and matches how the work is spread — one worker thread per document, each with its
own sequence of requests. Fakes in tests that replace `HTTPSConnection` keep working: a
response without `isclosed` is never pooled.

### 4. Default parallelism stays 1

ADR 0033 recommends 4 documents at once and `run_folder.ipynb` / `aia_wise.ipynb` already set
`MAX_PARALLEL_DOCUMENTS = 4`. The library default stays 1: N = 1 is the byte-for-byte serial
run that ADR 0022's budget tests and `FULL_STORE_DIGEST` pin, and N > 1 changes event order,
error handling (`Exception` per worker) and which document a tight total starves. Callers opt
in; `.env.example` names 4 as the recommended value.

## Consequences

- One client shared by several threads now has several requests in flight; ingest with
  N documents at once spends embedding time in parallel up to the semaphore.
- A keep-alive connection the server closes in the instant between the readability check and
  the send still fails as a connection error and is retried as a transient one (ADR 0035), as
  any connection error was before.
- Traces and records carry counts and timings only, as before; no body or header is kept.
- Tests: `tests/enterprise_pdf_rag/adapters/test_ingest_concurrency.py`.

## Rejected alternatives

- **A process-wide single-flight keyed by fingerprint alone.** Clients with different cache
  directories would wait for each other for no reason; across clients the cross-process claim
  (ADR 0023) already decides.
- **A shared, locked connection pool across threads.** More moving parts (checkout, limits,
  eviction) for no gain while each worker thread sends one request at a time.
- **Raising the semaphore for every embedder.** An injected embedder's thread safety is unknown
  and its shared counters would mix documents' requests.
