# ADR 0036: A sqlite object backend behind the stores, probed and never silently different

Status: Draft (PR-1 and PR-3 — the model cache — merged; store wiring lands in PR-2, visibility
and tooling in PR-4). Builds on
[ADR 0020](0020-storage-without-hard-links.md) (placement without hard links),
[ADR 0023](0023-claim-takeover.md) (claims with holders and leases),
[ADR 0024](0024-source-verification-cache.md) (per-instance verification) and
[ADR 0029](0029-sharded-store-layout-and-self-healing.md) (sharded layout, self-healing writes,
inline stage envelopes). It changes **no hash, no fingerprint, no envelope byte, no request
fingerprint and no published id** — only where small store entries live.

## Context

A 300-page report ingested on Databricks serverless (stores on Workspace files, a FUSE mount)
writes ≈ 43 000–50 000 files even after ADR 0029 Amendment 1, and every file costs a FUSE round
trip of 20–50 ms. Measured and extrapolated on the lite pipeline: the first ingest spends hours
in file placement alone, a fully-cached rerun still re-reads tens of thousands of files, and
`scan + mount` takes minutes. The entries are tiny (processing objects p50 ≈ 456 B, p90 ≈ 11 KB);
the cost is per-file, not per-byte. Workspace files also enforces 10 000 children per folder
(ADR 0029) and 500 MB per file, and its writes are flushed asynchronously, so files can be lost
after `close` returned (ADR 0029's self-healing exists because of that).

## Decision

### 1. A backend seam under the stores, two implementations

`ragspine.common.evidence.object_backend/` defines `ObjectBackend` / `ModelCacheBackend`
(protocol.py) — content-addressed objects, stage-cache entries, `current-*` pointers, mutable
records (document-tree), mount pins, a reentrant transaction scope; records / responses /
contexts / claims for the model cache — with two implementations:

- **`FileBackend` / `FileModelCacheBackend`** (files.py): today's layout, byte for byte. The
  three existing placement paths (`LocalDocumentStore.put`, `ProcessingStore`'s pointers and
  stage cache, `json_completion`'s immutable writes and claims) are carried over unchanged and
  pinned byte-identical by `tests/enterprise_pdf_rag/object_backend/
  test_file_backend_equivalence.py` (tree-by-tree compare, with and without hard links).
- **`SqliteBackend` / `SqliteModelCacheBackend`** (sqlite.py): one db per store root
  (`source/store.sqlite`, `processing/store.sqlite`, a `model-cache.sqlite` per cache dir).
  WAL, `synchronous=FULL` (configurable to NORMAL), `busy_timeout=30s`, `page_size=16384`,
  `application_id = 0x52535031` + `user_version = 1` as a version gate. Small objects, stage
  entries (envelope inline, Amendment 2 product column reserved), pointers, records and the
  model-cache tables live in the db; `INSERT OR IGNORE` + read-back + repair keeps ADR 0029's
  first-writer-wins and self-healing semantics, and restores the atomicity ADR 0020 lost on
  filesystems without hard links. JSON / text / SVG bytes ≥ 1 KiB are zlib-compressed in the
  row; **digests are always of the uncompressed bytes**, so no id moves.

### 2. What never enters the db

PDF originals (always external), and any object over `APP_OBJECT_STORE_INLINE_MAX_BYTES`
(262 144) or of a type in `APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES` (default `application/pdf`):
they still go through `FileBackend` into `objects/sha256-sharded/<ab>/`, with an index row
(`external=1`) in the db. Once the db exceeds `APP_OBJECT_STORE_MAX_DB_BYTES` (400 MiB, under
the Workspace 500 MB file cap) every new object over 16 KiB is externalized too.

### 3. Reads fall through, writes go to the db

Read order: db row → sharded file → legacy flat file (→ legacy `.claim` files for claims).
`FileBackend` is composed in as the read-only legacy layer, so a store written by any earlier
release keeps hitting without migration; enumeration takes the union. Writes always go to the
db (externals aside), and intact legacy entries are never rewritten.

### 4. Concurrency: leases, not sqlite file locks

sqlite's own file locking is not trusted on FUSE. Same-process threads use thread-local
connections and a contextvar transaction scope (reentrant; one `BEGIN IMMEDIATE` per outermost
scope). Across processes the writer side takes an `O_EXCL` lease file `<db>.writer`
(lease.py — the ADR 0023 holder JSON + lease + takeover generations, extracted and generalized;
since PR-3 `json_completion` reaches it only through the backends). A store db holds its lease for
the life of the process; a model-cache db holds it per transaction (§8). A live foreign holder
means `StoreBusy`
(reason code `store_busy`); a dead or out-of-lease holder is taken over. Readers take no lease:
every byte read is digest-verified anyway, and a torn read surfaces as `SQLITE_CORRUPT` or a
digest mismatch. Model-call claims move into a `claims` table with a generation-guarded
`UPDATE … WHERE generation=?` takeover; legacy `.claim` files are still honoured by their mtime
rule.

### 5. Corruption and crash behaviour

WAL frames carry checksums: a torn tail is dropped on recovery, losing only the last
transactions, with the main db intact (pinned by test: truncating the hot WAL loses exactly the
last transaction). A db that fails `quick_check` on open (or cannot be opened) is renamed
`store.sqlite.corrupt-<utc>` and rebuilt empty, counted as `note_repair("store_db")` — the next
run recomputes into it, exactly ADR 0029's stance that only writers holding the bytes repair.
Only corruption counts (PR-3): `SQLITE_CORRUPT`, `SQLITE_NOTADB` or a failed `quick_check`. A
busy or locked db (another process converting a new file to WAL, or writing) is retried for the
busy timeout and then raised — PR-1 renamed it as "corrupt", which silently sent the other
process's later writes into the renamed file (found by PR-3's two-process test: 30 of 60
records lost). The rebuild itself re-checks under the writer lease, so two processes never
rebuild each other's fresh db, and opening a db already at this version starts no write
transaction. The version gate reads `application_id`, `user_version` and the table count in one read
transaction (one snapshot) and re-reads them under `BEGIN IMMEDIATE` before creating the schema:
as separate autocommit reads, a peer's schema commit landing between them made a fresh db look
like a foreign one (`backend_schema_unmarked_database`). `close()` runs `wal_checkpoint(TRUNCATE)`.

### 6. Probe, settings and the no-silent-fallback rule

`probe.py` proves availability by building, writing, re-reading (a second connection; when
`-shm` is unusable, an EXCLUSIVE-locking single-connection candidate), `quick_check`-ing and
removing a real db in the target directory; failures carry a `sqlite_<step>` code only, never a
path, and results are cached per directory. `APP_OBJECT_STORE_BACKEND`:

- `auto` (default): probe passes → sqlite, otherwise the file layout;
- `sqlite`: a probe failure is `BackendUnavailable` with a message naming the code and how to
  set `files` — explicit choice is never silently degraded;
- `files`: today's layout, no probe.

### 7. PR split — and what PR-1 deliberately does not do

1. **PR-1 (this change): the backend package, settings fields and the two-backend conformance
   pack only.** Nothing is wired: no store, no `json_completion` call site changes, so there is
   **no behavior change** anywhere — `FULL_STORE_DIGEST` and every regression pass untouched,
   and the default `auto` has no reader yet. This keeps the PR conflict-free against the
   parallel storage-layer branches.
2. PR-2: store wiring (`LocalDocumentStore` / `ProcessingStore` / `scan_catalog` / mount pins /
   transaction scopes; layout-coupled tests parametrized).
3. PR-3: model-cache wiring (`JsonCompletionClient(cache_dir, backend=…)`; ADR 0021/0023/0029
   tests parametrized; lease.py replaces the private claim family) — §8.
4. PR-4: visibility (`object_backend` in summaries / events / report), `store probe|migrate|
   export` tooling, the benchmark script, and this ADR's Measured section.

### 8. PR-3: the model cache

`JsonCompletionClient(config, cache_dir=…, backend=None)` reads and writes its cache only
through a `ModelCacheBackend`, opened on first use by `open_backend(cache_dir, "model-cache")`
(`auto` probes `cache_dir`); building a client touches no file. `client.backend_kind` says
which (`"sqlite"` / `"files"`); the `object_backend` report fields are PR-2 / PR-4's.

- **Call sites.** The record lookup (`<fp>` / `<fp>.retry-1` keys), immutable record writes
  (`StoreConflict` → `cache_conflict`; `replace_damaged` for ADR 0029 repairs and ADR 0035
  stale transient records), response writes (content-addressed, a damaged one replaced),
  context writes (first stored body wins; an existing row or legacy file is a no-op without a
  write transaction), response reads (`DamagedEntry` → `cached_response_digest_mismatch`),
  the claim (`claim(key, owner, expired=…)`), the ADR 0035 renewal before each retry
  (`renew(key, owner, generation)`), the release, and `_save_skip`'s "no skip record under a
  claim" check (`claimed(key)`) are backend calls. The record bytes are identical in both
  backends — `context_path` stays the logical `contexts/<fp>.json` — so the full-mode
  `FULL_STORE_DIGEST` / `FULL_REQUESTS_DIGEST` hold on sqlite when the model cache is read back
  logically (`test_parallel_documents`).
- **One judge.** `json_completion._expired` still decides whether a holder is over; the
  backend calls it once per claim attempt (outside the compare-and-set): for a claim file with
  its mtime, for a `claims` row with its `created_at`. Taking over is a compare-and-set in both
  backends — `O_EXCL` of `.claim.takeover-<n+1>`, or `INSERT OR IGNORE` / `UPDATE … WHERE
  generation = ?` — so of racing callers exactly one wins. Renewal is the same compare-and-set
  from the holder's own generation.
- **Legacy entries.** On sqlite the lookup order is `requests` row → `requests/<key>.json` (the
  `.retry-1` variant too), and likewise for responses and contexts; claims: `claims` row → the
  highest legacy `.claim[.takeover-<n>]` file, judged by its mtime rule and taken over at
  generation n+1 (a row), the files removed on release as the files layout does. Intact legacy
  files are never rewritten or moved; a damaged one is superseded by a row (the file stays).
  A backend instance probes each legacy directory once, so a cache that never had one costs no
  file operation for read-through.
- **Transactions.** One per write: claim, context, response, record, release — five per live
  call, and none for a replay (opening an existing db writes nothing). Nothing joins a store's
  page transaction.
- **The writer lease is per transaction** for a model-cache db: the root-level answer cache
  (`<ingestion_root>/model-cache/`) may be written by several processes (a notebook kernel and
  the API), and a process-long lease would lock every other one out with `store_busy` for the
  life of the first. Same-process transactions queue on a per-db-path lock; across processes
  each transaction creates a non-durable `<db>.writer` lease (no fsync: losing it in a crash
  only frees it), waits up to the busy timeout, and removes it at commit. A process killed
  inside a transaction leaves the lease file with a dead pid: the next writer takes it over at
  once (ADR 0023's rule), and sqlite rolls the half transaction back.
- **Crashes.** A child killed mid-transport leaves a `claims` row, taken over once its pid is
  gone or its lease over (the ADR 0023 subprocess test runs on both backends); a WAL tail torn
  inside the last call's first frame loses exactly that call, which is simply made again; a db
  that is not a database is set aside and rebuilt, counted `storage_repairs.store_db`.
- **Tests.** The seven model-cache test modules (`test_json_completion`, `…_sampling_fallback`,
  `…_claim_takeover`, `…_model_cache_self_heal`, `…_transient_provider_errors`,
  `…_parallel_documents`, `…_folder_claim_recovery`) run every case on both backends through the
  `model_cache_backend` fixture; `tests/conftest.py` pins `APP_OBJECT_STORE_BACKEND=files` for
  the rest of the suite (export it to run everything under `auto`).
  `test_model_cache_backend_wiring.py` pins the selection, the Volumes-like fallback, legacy
  read-through without migration, the transaction count, two processes sharing one db, and the
  crash cases above. The files-layout equivalence pack now compares against a frozen copy of the
  pre-PR-3 write path (`tests/enterprise_pdf_rag/object_backend/legacy_model_cache.py`).

## Weaker / unverified

- **Real Databricks Workspace-files semantics are unverified** (locks, `-shm`, fsync): the
  design assumes the probe is the arbiter per directory and the writer lease carries mutual
  exclusion; first-party measurement happens before the default flips anywhere that matters.
- The writer lease is process-wide and time-boxed (1 h, pid-aware takeover): a pathologically
  slow holder past its lease can be overtaken, as with ADR 0023 claims; digest verification
  bounds the damage to a refused read / re-done write.
- `pin_unchanged` uses `data_version` + db / WAL stat as the cheap check; a backend that
  cannot stat falls back to re-reading and re-hashing the row.
- Rolling back to a pre-backend release cannot read db-resident objects: it recomputes and may
  re-send model calls. `store export --to files` (PR-4) is the escape hatch.
- ADR 0029 Amendment 2 (inline stage outputs) landed on `main` while this PR was built:
  `FileBackend`'s third pointer segment is byte-identical to `split_stage_pointer`'s format
  (`<digest>\n<envelope>\n<output>`), pinned against `ProcessingStore.cache_output` by the
  equivalence pack; the sqlite `stage_cache.product` column carries the same bytes.

## Measured

Store-level numbers are deferred to PR-2 / PR-4. **PR-3, the model cache** (developer Mac, not
Databricks): 600 scripted calls with distinct prompts, then a replay round by a new client,
every file-system syscall counted (and, in a second run, delayed) by a `DYLD_INSERT_LIBRARIES`
interposer, so sqlite's own C-level I/O is counted like Python's. `-shm` is mmap'd and costs no
syscall; the fake sender returns one body, so `responses` holds a single entry.

| backend | round | files left | syscalls (per call) | of which fcntl locks | wall, 0 ms | wall, +5 ms / syscall* |
|---|---|---|---|---|---|---|
| files | first (600 live) | 1 201 | 29 393 (49.0) | 0 | 1.65 s | 215 s |
| files | replay | 1 201 | 8 400 (14.0) | 0 | 0.36 s | 62 s |
| sqlite | first (600 live) | 1 (3 open) | 60 741 (101.2) | 22 894 | 1.31 s | 445 s |
| sqlite | replay | 1 (3 open) | 7 476 (12.5) | 4 851 | 0.30 s | 55 s |
| sqlite, no `.writer` lease | first | 1 (3 open) | 36 737 (61.2) | 22 894 | 0.83 s | 268 s |

\* `usleep(5000)` measured 7.4 ms here. Five write transactions per live call (3 001 in all),
none in the replay round. The per-transaction `<db>.writer` lease is 8 syscalls a transaction,
39.5 % of the first round. Every model-cache file of three earlier generations (`b989625`,
`3414e0c`, `f577170`, each a full-mode folder of 24 calls, 68 files) reruns under sqlite with
0 live calls, the same published id, no repair and no row written — also with every stage
cache dropped, when all 24 calls are answered from the old files.

Reading: sqlite turns 1 201 files into one db and makes a replay slightly cheaper, but a first
round of live calls costs about twice the syscalls of the files layout, mostly sqlite's own
WAL locking plus the lease. On a FUSE mount where every syscall is a round trip that is slower;
next to the seconds a real model call takes, it is ≈ 0.3 s per call at 5 ms. Whether fcntl
locks are round trips on Workspace files decides most of that and is unmeasured. Cheaper
options, not taken here: one transaction for response + record + release (5 → 3), or no
`.writer` lease where the probe proves sqlite's own locks.

## Rejected alternatives

- **One db per ingestion root**: a single writer serializes every document, one corruption has
  maximal blast radius, and Workspace's 500 MB file cap is reached by a handful of documents.
- **A custom pack file**: workable on Volumes but a bespoke format with weaker atomicity and
  its own recovery tooling; sqlite's WAL already is the tested implementation of that idea.
- **Double-writing pointers to db and file**: two sources of truth that can disagree; the
  fallback for an unreadable db is rebuild-and-republish, not a shadow copy.
- **Externalizing by media type alone**: object size is what threatens the db cap; type is only
  a hint (`application/pdf` stays a hard rule).
- **`PRAGMA locking_mode=EXCLUSIVE` everywhere / `immutable=1` readers**: breaks concurrent
  readers during ingest; EXCLUSIVE remains only the probe's `-shm` fallback verdict.
- **Trusting sqlite file locks on FUSE**: the field evidence behind ADR 0020/0029 says
  filesystem semantics there are exactly what cannot be assumed.
