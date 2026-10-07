# ADR 0023: A model-call claim left by a dead attempt is taken over, and released once recorded

Status: Accepted, 2026-10-05. Amends the model-cache
claim of [ADR 0011](0011-document-catalog-and-verified-answer-chain.md) (`retry_failed=False`,
"uncertain attempts stay claimed"), the claim paragraph of
[ADR 0020](0020-storage-without-hard-links.md) and the "a `.claim` without a record still
blocks" bullet of [ADR 0021](0021-sampling-parameter-fallback.md) §4. Request fingerprints,
record and response bytes, and the replay of every existing record are unchanged; old cache
directories (legacy claims and records) are used as they are, with no migration.

> Amended by [ADR 0035](0035-transient-provider-errors.md) (2026-10-06): a call now retries a
> transient failure up to three times, pausing up to `RETRY_MAX_PAUSE` = 31 s before an attempt.
> The holder **renews** its claim before each retry by exclusively creating the next generation
> (`.takeover-<n+1>`, the §3 primitive; losing that create stops the call unsent), so a lease
> still covers one attempt: `lease_seconds = ceil(4 × timeout + 31) + 120` — **331 s** at 45 s
> (was 300), **871 s** at 180 s (was 840). `LEGACY_CLAIM_LEASE_SECONDS` stays 900: above both, and
> above the 840 s a legacy writer (which never paused or retried) could run. The numbers in §1,
> §2 and the Consequences below are the pre-ADR-0035 ones.

## Context

`JsonCompletionClient` serializes a live call by creating `requests/<fp>.json.claim` with
`O_CREAT | O_EXCL` (file fsync + directory fsync) **before** the request is sent, and writes the
record `requests/<fp>.json` after the reply. Records are checked first, so a claim never blocks a
replay. But a claim without a record meant "another attempt may be in flight, or was in flight
and may have been billed": every later call on that fingerprint failed with
`request_in_progress_or_uncertain` and sent nothing, and nothing ever removed the claim.

That is right while the holder runs; it is wrong forever after. A user running
`notebooks/run_folder.ipynb` on Databricks had kernels killed and interrupted mid-call. The
attempt in flight left a claim and no record; every rerun then failed that page's layout call
(and its metadata), the document still came out `published` with `error=None`, and the page
was missing silently and permanently until someone deleted the claim by hand
(reproduced offline: interrupt the 3rd send of a 5-page PDF, rerun → `layout_ok=4`, the page's
diagnostic `request_in_progress_or_uncertain`, every later rerun identical).

Two facts bound the problem:

- **A holder's call has a bounded life.** `JsonCompletionClient` refuses `timeout > 180`. That
  timeout bounds each blocking socket operation (connect, send, waiting for the status line,
  reading the body), not the call as a whole, so a call that is still running after roughly four
  timeouts plus the cache writes around it is not a call that can still deliver a record.
- **The claim had no holder.** Its content was the fingerprint (older ones are empty), so
  nothing on disk said who made it or when, except its mtime.

## Decision

### 1. A claim names its holder and its lease

A new claim is a small JSON document (no prompt, key or body):

```json
{"claim": "json-completion-claim-v2", "request_fingerprint": "<fp>",
 "host": "<socket.gethostname()>", "pid": 4242, "process": "<uuid4 hex, one per process>",
 "created_at": 1791200000.123, "lease_seconds": 840}
```

`lease_seconds = ceil(4 × timeout) + 120` of the client that made it (300 s at the default 45 s;
840 s at the 180 s cap the ingestion and tree clients use). `created_at` is the holder's wall
clock.

### 2. When a holder is certainly over (`_expired`)

- **Current-format claim, holder on this host and gone:** `host` equals ours, `process` is not
  ours and `os.kill(pid, 0)` raises `ProcessLookupError` → over at once (a restarted kernel on
  the same driver recovers immediately). POSIX only: on Windows `os.kill(pid, 0)` would signal
  the process, so it is never probed there. A reused pid, `EPERM`, another host or our own
  process token all fall through to the lease.
- **Current-format claim, otherwise:** over once `now − created_at > lease_seconds`. This is the
  rule that does not depend on pids, so it is what decides across hosts (a cluster restart gives
  the driver a new hostname) and inside one process (a notebook Interrupt raises
  `KeyboardInterrupt` through the transport and leaves this process's own claim behind).
- **Legacy claim** (fingerprint only, empty, unparseable, or a current-format claim without a
  usable `created_at` / `lease_seconds`): over once `now − mtime > LEGACY_CLAIM_LEASE_SECONDS`
  (900 s). Nothing says how long its holder may run, so it gets more than the largest current
  lease (840 s) plus a minute. A process still running the old code is therefore never overtaken
  in the middle of a call: no old call outlives four timeouts plus its writes.

Clock assumptions: hosts sharing a cache directory keep wall clocks within a minute of each other
(NTP); for legacy claims the filesystem's mtime is in the same time base as the reader's clock.
A clock that runs ahead by more than the margin can overtake a live holder early (cost below);
one that runs behind only delays recovery. A freshly created holder file read before its content
was written looks like an empty legacy claim with a fresh mtime, i.e. alive.

### 3. Taking over is an exclusive create, never a rename or replace

Holders form generations: `<fp>.json.claim` (0), then `<fp>.json.claim.takeover-1`, `-2`, … The
current holder is the highest generation present (probed in order, no directory listing). A
caller that fails to create the base claim reads the current holder; if it is over, it creates
the next generation with `O_CREAT | O_EXCL` (file fsync + directory fsync, as before). Of several
callers that judged the same holder over, exactly one creation succeeds; the rest get
`request_in_progress_or_uncertain`, as does every caller while the holder may still run. A caller
that saw an older generation tries to create one that already exists and fails the same way.

This needs only the primitive the claim already relied on and that the target FUSE mount
supports. It deliberately avoids `link_new_file` (on a filesystem without hard links it is a
check-then-`os.replace`, which overwrites a concurrent winner) and `os.rename` of the claim
(a contender can rename away a claim another process has just re-created, losing it).

Gaps: the walk stops at the first missing generation; files are only ever removed newest-first
(§5), so a gap never hides a live holder.

### 4. A takeover is one real call, and says so

After a takeover the request is sent again exactly like a first attempt: one unit of
`max_live_calls` / `live_call_count` (none is spent if the budget is already exhausted, and the
claim is then left as it was). Its record carries `diagnostics.claim_takeover = <generation>`, a
new optional field written only on such records (`exclude_unset`), so every other record stays
byte-identical and every old record still parses.

**Cost.** The dead holder may already have reached the provider, so the request can be billed
twice. That is accepted because it only happens after the holder is dead or out of lease. If a
holder outlives its lease anyway (clock skew, a pathologically slow transfer), both attempts can
send; the first record written wins (`_immutable_write`), the other attempt reports
`cache_conflict`; on a filesystem without hard links this first-writer rule is best effort, as
ADR 0020 describes.

### 5. A claim is released once its record exists

After writing the record (success or failure) the holder deletes its claim files, newest
generation first. A record is checked before any claim, so the record alone answers from then
on; this halves the `requests/` file count (Databricks Git folders advise fewer than 20 000
files). A failed deletion only leaves a harmless claim beside its record.

Release makes one race possible: a caller that looked for the record just before it was written
can then create a fresh claim. So a caller re-checks the record **after** acquiring a claim, and
also after failing to acquire one; if the record is there it releases what it holds and replays
it instead of sending. A record written before a deletion is always visible to a claim created
after that deletion.

Old data: claims left beside existing records by the old client are never touched (only the
claim files of the record a call just wrote are released); the `.retry-1.json` re-probe of
ADR 0021 has its own claim, released the same way.

### 6. Interaction with ADR 0021

- The refused original, the dropped fingerprint and the `.retry-1.json` re-probe each claim their
  own record path and are taken over and released independently; a takeover that is refused for
  `temperature` writes its 400 record (with `claim_takeover`) and redirects as before.
- A skip record (`sampling_parameter_unsupported`) is still never written where a base claim
  exists: a call that knows the refusal goes on to the dropped fingerprint without touching the
  claim; a later process without that memory takes the claim over once it is expired, probes once
  and records.
- Recursion is bounded: the replay after a re-check finds a record, so it replays or, for a
  failure record with `retry_failed`, moves on to `.retry-1.json`, whose record then answers.

### 7. Visibility

`JsonCompletionClient.claim_blocked_count` (calls that ended `request_in_progress_or_uncertain`)
and `claims_taken_over`; `IngestionSummary.calls_claim_blocked` / `claims_taken_over` carry the
ingestion client's counts into `DocumentRun.ingestion` and the folder result. Counts only, no
fingerprints or text. A page that fails this way is still a failed stage; `published` keeps its
meaning.

## Consequences

- A page lost to an interrupted call recovers on a rerun with no manual step: at once when the
  dead process ran on this host (POSIX), otherwise once the lease has run out (≤ 14 min for the
  180 s clients, 5 min at 45 s); a legacy claim recovers once it is 15 minutes old.
- Until then the failure is counted in the run result instead of being silent.
- Claims written by this version are JSON; an old client reading the directory never parses a
  claim, so nothing breaks, but it would treat these claims as forever in progress (as it did).
- `RequestDiagnostics` gains a field; a new record with `claim_takeover` is rejected by an old
  client's strict model (`extra="forbid"`). Rolling back the code therefore needs those records
  deleted; they only exist where a takeover happened.

## Rejected alternatives

- **Pid liveness only** (claim takeover only for same-host dead pids): after a cluster restart the
  hostname changes and the claim would stay stuck forever; legacy claims (all of the user's) would
  never recover.
- **A heartbeat lease** (the holder rewrites the claim while it waits): needs a thread per call
  and repeated whole-file rewrites on FUSE; the 180 s cap already bounds a call.
- **An explicit cleanup function only** (delete every claim without a record): correct only when
  no other process uses the cache, which the code cannot know; kept out of the hot path.
- **Recovery inside the same process right after an Interrupt** (our token, not in flight here):
  an uncertain attempt would be resent at once, which `test_uncertain_inflight_claim_survives_and_cannot_be_silently_retried`
  forbids; the lease applies to this process too.

## Validation

`tests/enterprise_pdf_rag/adapters/test_claim_takeover.py` (24 tests: claim content and release,
lease and legacy thresholds with an injected clock / `os.utime`, a real child killed with
`os._exit`, a dead takeover taken over again, two threads racing behind a barrier with and
without hard links → exactly one send, a holder finishing between look and claim → replay,
budget, ADR 0021 refusal and `.retry-1` re-probe after a takeover) and
`test_folder_claim_recovery.py` (an interrupted folder run: blocked and counted within the lease,
recovered and `published` complete after it, zero calls on the next rerun; the same with a
legacy claim). Existing claim tests are unchanged except two layout assertions that listed the
now released claim file.
