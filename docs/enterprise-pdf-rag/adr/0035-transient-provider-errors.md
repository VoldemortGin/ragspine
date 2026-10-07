# ADR 0035: Rate limits and server errors are retried, never cached as a permanent failure

Status: Accepted, 2026-10-06. Amends the failure records of
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) / [ADR 0021](0021-sampling-parameter-fallback.md)
(every failed live call is recorded and replayed), the claim lease of
[ADR 0023](0023-claim-takeover.md), the batch degradation of
[ADR 0026](0026-batched-embeddings.md), and closes the 429 risk named in
[ADR 0033](0033-parallel-documents.md)'s recommended setting. Fingerprints, response bytes and
every success record are unchanged byte for byte (`FULL_STORE_DIGEST` holds); a request that
succeeds at its first attempt behaves exactly as before.

> Amended by [ADR 0036](0036-sqlite-object-backend.md) §8 (2026-10-06, PR-3): the renewal
> before each retry (§5) is `backend.renew(key, owner, generation)` — the next
> `.claim.takeover-<n>` file, or the `claims` row moved to the next generation only if it
> still holds this caller's generation; losing it still ends the call unsent. A stale
> transient record is replaced by `replace_damaged` in either backend (on sqlite, a legacy
> file is superseded by a row, never rewritten). Its tests run on both backends.

## Context

Every failure of a model call was written to `requests/<fingerprint>.json` and replayed by every
later run without a request: `provider_http_429`, `provider_http_503`, `provider_timeout`,
`provider_connection` just like a 401 or a malformed reply. One rate-limited response therefore
failed its page **for good** — reruns replayed the 429 — until someone deleted the record by
hand. ADR 0033 made this likelier (N documents send N requests at once) and recommended keeping N
where the deployment never answers 429. Embeddings had the opposite problem: ADR 0026 halved a
failed batch at once, so a 429 on a batch of 16 turned into up to 31 more requests against an
endpoint that had just asked for fewer.

These failures say nothing about the request; the same bytes may well succeed seconds later.

## Decision

### 1. Classification (`providers/transient.py`)

| Failure | Class | `failure_code` |
|---|---|---|
| HTTP 408, 429, 500, 502, 503, 504 | **transient** | `provider_http_<status>` |
| timeout (`ProviderRequestError.category == "timeout"`, a raw `TimeoutError`) | **transient** | `provider_timeout` |
| connection error (`category == "connection"`, any other raw `OSError`) | **transient** | `provider_connection` |
| HTTP 400 (outside the ADR 0021 sampling refusal), 401, 403, 404, 409, 413, 422, 501, any other status | permanent | `provider_http_<status>` |
| response over the size limit, invalid / truncated / refused reply, schema failure | permanent | as before |

`TRANSIENT_HTTP_STATUSES`, `TRANSIENT_CATEGORIES` and `TRANSIENT_FAILURE_CODES` are the named
sets. For a model call the decision is made on the very `failure_code` a record would carry, so
classification and record can never disagree.

### 2. Retried within the call

A transient failure is retried in the same `complete_json` / `complete_text_json` call:

- at most `TRANSIENT_MAX_RETRIES = 3` retries (4 attempts);
- without `Retry-After`: `min(30 s, 1 s · 2^n)` with equal jitter (between half and all of it) —
  about 1, 2, 4 s;
- with `Retry-After`: what the endpoint asked, capped at `RETRY_MAX_DELAY = 30 s`, plus up to one
  base delay of spread. `_send_once` (and `_send_local_once`) read the `retry-after-ms` header
  (Azure OpenAI) or `Retry-After` (delta-seconds or HTTP date) of a non-200 response — **headers
  only**: a non-400 body is still never read, and the value is a number, never logged;
- **every retry is a live call**: it spends one unit of `max_live_calls`, so the budget of
  ADR 0022 / 0033 still bounds what a run can spend. A retry that finds the budget empty is not
  sent; the call ends with the transient code;
- the request body is the same bytes: the fingerprint, the `contexts/` file and the ADR 0021
  sampling-parameter state do not move (a 400 refusal, permanent, is handled as before; a 429 on
  the dropped request is retried like any other).

`retry_count` (requests resent) and `transient_failure_count` (calls still failing transiently
once their retries or budget were spent) count both on `JsonCompletionClient` and on
`LocalEmbeddingAdapter`.

### 3. No record for a transient failure

A call that still fails transiently **writes no record** and releases its claim. The page is
FAILED for this run (its stage diagnostic carries `provider_http_429` etc., as before), and the
next run simply calls it again — there is nothing to delete.

The alternative, a record flagged `transient: true` that reruns read as "not cached", was
rejected: it would be one more record kind for ADR 0021's redirect / skip / re-probe logic to
step around, an old client would reject it (`extra="forbid"`), and it buys nothing a missing
record does not already say. ADR 0029's self-healing ("record present, response missing → call
once more") is unaffected: with no record, the next run is an ordinary first call. A record is
still written for every permanent failure, so permanent failures stay cached exactly as before.

`cache_only` callers see `cache_miss` where they used to see a replayed transient failure.

### 4. Records written before this ADR

A record whose `failure_code` is in `TRANSIENT_FAILURE_CODES` (any `provider_http_429`,
`provider_timeout`, ... left by an older client) is **called again** instead of replayed, at the
same path, under a claim:

- success or a permanent failure **replaces** the old record (the replace path of ADR 0029);
- a new transient failure leaves the old record as it is, for the next run to try again;
- if the in-process ADR 0021 memory already knows the body carries a refused parameter, the call
  goes on to the dropped fingerprint without sending the old body;
- `cache_only` still replays it (never sends);
- a live claim beside it blocks (`request_in_progress_or_uncertain`), it never loops. A client
  older than ADR 0023 never released its claims, so such a record usually sits beside a legacy
  `.claim`; that one is long past `LEGACY_CLAIM_LEASE_SECONDS`, so the re-call takes it over
  (counted in `claims_taken_over`, record marked `claim_takeover`) and heals the entry.

So the user's pages that an earlier run lost to a rate limit heal on a plain rerun.

### 5. Claims: held through the retries, renewed before each one

The claim of ADR 0023 is held from before the first attempt until the call's outcome is
recorded (or, for a transient failure, until it ends) — no other process can send the same
request in between. A call can now last up to four attempts and three backoffs; rather than
quadrupling the lease (which would have made an interrupted kernel's page wait about 52 minutes
instead of 14 before a takeover), the holder **renews** its claim before each retry by creating
the next claim generation (`<fp>.json.claim.takeover-<n+1>`) — the same `O_EXCL` create a
takeover uses, so of a renewing holder and a contender exactly one wins. A holder that loses (a
contender judged it dead, which only happens past its lease) stops without sending again
(`request_in_progress_or_uncertain`, counted in `claim_blocked_count`) and leaves the new
holder's claim alone. Release removes every generation, newest first, as before; a renewal is
not a takeover, so the record carries no `claim_takeover`.

The lease therefore covers **one** pause and one attempt:
`lease_seconds = ceil(4 × timeout + RETRY_MAX_PAUSE) + 120`, `RETRY_MAX_PAUSE = 31 s` →
**331 s** at the 45 s default (was 300), **871 s** at the 180 s ingest / tree clients (was 840).
`LEGACY_CLAIM_LEASE_SECONDS = 900` still exceeds both, and still exceeds the 840 s a pre-ADR-0023
writer (which never paused or retried) could run.

### 6. Documents at once: jitter, and one shared cooldown

The jitter keeps workers that failed together from retrying together. A `Retry-After` is in
addition shared: a process-wide registry keyed by `(URL, model)` — the key of ADR 0021's
sampling registry, guarded by its own lock — records "do not send before t", and every caller of
that endpoint pauses until then (plus up to one base delay of spread) before its next attempt,
first attempts included. One pause never exceeds `RETRY_MAX_PAUSE`. The registry lives in
`providers/transient.py` beside the classification rather than in `json_completion.py`, because
the embedding adapter uses it too; chat and embeddings of one gateway have different keys and do
not cool each other down. `forget_cooldowns()` clears it (an autouse test fixture does).

### 7. Embeddings (ADR 0026)

`LocalEmbeddingAdapter._post` retries a transient failure the same way before anything else:
a rate-limited batch is sent **whole** again, never halved. Only once its retries are spent does
the ADR 0026 degradation apply (halve, down to single inputs; the batch counts towards
`EMBEDDING_BATCH_MAX_FAILURES`), because a timeout on a large batch can still be its size. 401 /
403 / 404 are still raised at once, and a 400 on a pair still marks arrays unsupported.
`request_count` counts every request, retries included. Under ADR 0033 the per-document
embedder view serialises requests, so a backoff holds that lock: the other documents' embedding
requests wait the same cooldown they would have hit anyway.

### 8. Visibility

- `IngestionSummary.retries` / `transient_failures` (the ingest client), and
  `DocumentRun.retries` / `transient_failures` (ingest + index embeddings + tree);
- the `document_done` event carries `retries` and `transient_failures` beside `pages`;
- `report.md` has one line when any document retried or still failed transiently;
- `notebooks/run_folder.ipynb` shows both columns and, for a document with transient failures,
  says the page is unfinished this round and that a rerun retries it, with no file to delete.

Counts only: no body, header value, URL or text reaches a record, event, report or trace.

## Consequences

- A rate limit or a 5xx costs some waiting and up to three extra billed requests per call, and a
  rerun instead of a manual cleanup; it no longer loses a page for good.
- A call that keeps timing out on a 180 s client can now take about 4 × 4 × 180 s in the worst
  case before its page is given up for the run; the claim is renewed throughout.
- `max_live_calls` is consumed faster on a rate-limited endpoint; a run under a tight budget can
  end `call_budget_exhausted` / `budget_starved` sooner.
- The only on-disk differences are the absence of transient failure records, extra
  `.claim.takeover-<n>` files while a call retries (released with the record), and old transient
  records being replaced. An old client reading the directory sees ordinary records and claims.
- ADR 0033's advice changes from "keep N where the endpoint never answers 429" to "lower N if
  `retries` / `transient_failures` are frequent".

## Rejected alternatives

- **Retry across runs only** (no in-call retry, just no record): a page waits for the next
  run even when the endpoint recovered in a second.
- **A transient record read as uncached** (§3).
- **A longer lease instead of renewal** (§5): 52 minutes before an interrupted page recovers.
- **Splitting a rate-limited batch** (ADR 0026 as it was): multiplies the requests that are
  being refused.
- **Retrying every failure**: a 400 / 401 / 404 / 422 fails the same way every time and would
  only spend budget.

## Validation

`tests/enterprise_pdf_rag/adapters/test_transient_provider_errors.py` (classification, header
parsing, backoff, retry with live-call and budget accounting, `Retry-After` honoured and capped,
exhausted retries leave no record and the next run calls again, claim held and renewed, a
renewal lost to a contender, permanent statuses unchanged, old 429 / 503 / timeout / connection
records retried and replaced or kept, ADR 0021 interplay, shared cooldown per endpoint and model,
jitter, embedding retry before split, a folder run that counts, reports and heals by rerun), with
`test_json_completion.py`, `test_sampling_fallback.py`, `test_claim_takeover.py`,
`test_embedding_batches.py`, `test_description_correction.py` and
`test_document_tree_extraction.py` updated where they pinned a transient failure as permanent
(they now use a permanent failure for the same mechanism, or pin the retry), and
`test_run_folder_notebook.py` for the notebook.
