# ADR 00NN: A sqlite object backend behind the stores, probed and never silently different

Status: Draft (PR-1 merged; wiring lands in PR-2/3, visibility and tooling in PR-4). Builds on
[ADR 0020](0020-storage-without-hard-links.md) (placement without hard links),
[ADR 0023](0023-claim-takeover.md) (claims with holders and leases),
[ADR 0024](0024-source-verification-cache.md) (per-instance verification) and
[ADR 0029](0029-sharded-store-layout-and-self-healing.md) (sharded layout, self-healing writes,
inline stage envelopes). It changes **no hash, no fingerprint, no envelope byte, no request
fingerprint and no published id** — only where small store entries live.

> 编号说明:`00NN` 在 rebase 到最新 main 时按当时的最大 ADR 编号 +1 定号(并行分支也在写
> ADR,编号最后定)。

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
`json_completion` keeps its own copy until PR-3). A live foreign holder means `StoreBusy`
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
`close()` runs `wal_checkpoint(TRUNCATE)`.

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
   tests parametrized; lease.py replaces the private claim family).
4. PR-4: visibility (`object_backend` in summaries / events / report), `store probe|migrate|
   export` tooling, the benchmark script, and this ADR's Measured section.

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
- Amendment 2's inline product column / third pointer segment is reserved to the design's
  `StageEntry.product` shape; the exact on-disk third-segment format is aligned with `main`
  when that amendment lands.

## Measured

Deferred to PR-4 (the wiring PRs carry the pipeline-level numbers; PR-1 has only
micro-benchmarks of the backends themselves, recorded in the PR discussion, measured on a
developer Mac, not Databricks).

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
