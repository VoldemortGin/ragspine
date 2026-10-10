# ADR 0044: An opt-in staged object backend — local working copy, whole-file publish per stage

Status: Proposed (opt-in, default off). Builds on
[ADR 0036](0036-sqlite-object-backend.md) (the `ObjectBackend` seam and the sqlite backend),
[ADR 0029](0029-sharded-store-layout-and-self-healing.md) (sharded layout, self-healing) and
[ADR 0034](0034-persisted-verification-receipts.md) (receipts). It changes **no hash, no
fingerprint, no envelope byte and no published id** — only where the store db is read and
written while a document is being ingested.

**Whether to enable it is not decided here.** It waits for `notebooks/ingest_timing.ipynb`
run on Databricks against the real ingestion root (Workspace files, FUSE) and against
`/local_disk0`: its `store_write` bucket and its `fileops/page` / `ms/file` line say how much
of an ingest is file-system time on FUSE. On the developer Mac the store is ≈ 5 % of the
wall time and one file operation costs ≈ 0.04 ms; the FUSE numbers are **unmeasured**.

## Context

On Databricks the ingestion root lives on Workspace files: about 2 900 file operations a page
on a first run and 860 on a rerun, 23–26 k files for a 300-page document, each operation a
FUSE round trip. ADR 0036 folds the small entries into one `store.sqlite` per store root, but
puts that db **on the FUSE mount**, and records that fcntl locks and the WAL `-shm` on
Workspace files are unverified — the probe is the arbiter, and a sqlite db with random
writes and locks on FUSE is exactly what cannot be assumed to be fast or safe.

## Decision

### 1. `StagedBackend` (object_backend/staged.py)

A `SqliteBackend` whose db lives in a **local working directory**
(`<APP_OBJECT_STORE_STAGING_DIR>/<sha256(store root)[:32]>/store.sqlite`, default
`<tempdir>/ragspine-staged`, on Databricks `/local_disk0/ragspine-staged`). Every db read and
write is local. `root` stays the store root (the FUSE directory): external objects (the PDF,
objects over the inline cap) and the legacy file layout are still read and written there by
the composed `FileBackend`, so `object_location` / `content_path` / receipts keep pointing
inside the store root. `kind = "staged"`.

- **`commit()`** — if anything was written since the last commit: `sqlite3` online backup of
  the committed state into a local snapshot (`journal_mode=DELETE`, one self-contained file),
  sequential copy to `<root>/.store.sqlite.staging-<pid>-<rand>`, `fsync`, `os.replace` onto
  `<root>/store.sqlite`, directory fsync (best effort). A process killed before the rename
  leaves only the temporary file; the published version is untouched, and the next commit
  (holding the publisher lease) removes the leftover. Nothing written → no file touched.
- **Resume** — on open, if there is no local copy, or the published file's (size, mtime_ns)
  differs from what this working directory last published or copied, the published file is
  copied back whole (`counts["restores"]`). Otherwise the local copy is kept: it may hold
  progress a crashed process never published; a content signature (row count + latest write
  time per table) recorded at each commit tells, and a differing one makes the next commit
  republish.
- **Mutual exclusion** — before its first write an instance takes `<root>/store.sqlite.publisher`
  (the ADR 0023 holder JSON + lease + takeover generations, `lease.py`, process-scoped, 1 h
  like the ADR 0036 writer lease); a live foreign holder → `StoreBusy("store_busy")`. Released
  on the final close.
- **Privacy** — `counts` holds counts and milliseconds only (`commits`, `published_bytes`,
  `commit_ms`, `restores`, `restored_bytes`, `restore_ms`, `commit_failures`); errors carry
  fixed texts / reason codes; the working directory name is a hash, not the path.

### 2. One instance per store root per process

The stages each open their own stores (`ingest_pdf`, `qualify_draft`, `index_draft`,
`publish_draft` take paths). In staged mode `open_backend(root)` returns the **shared**
instance from an in-process registry (`acquire_staged`); a store's `close()` only drops its
reference. `commit_staged(prefix)` commits every registered instance under a directory,
`release_staged(prefix)` commits, unregisters and drops the registry's reference (the last
reference closes the local db and releases the lease). An `atexit` hook commits whatever is
still registered (failures counted, the local copy kept for the next run).

### 3. Selection and wiring

`APP_OBJECT_STORE_BACKEND=staged` (new value; default stays `auto`), plus
`APP_OBJECT_STORE_STAGING_DIR` (default unset). The working directory is probed like ADR 0036
§6; a failed probe is `BackendUnavailable`, never a silent fallback. The **model cache** stays
on the file layout in staged mode (the root-level answer cache is written by several
processes; staging it would need cross-process merging).

`folder_pipeline._run_document` calls `commit_staged(<ingestion root>/<pdf sha256>)` at every
stage boundary (`enter()`: before requalify, qualify, index, publish, tree), once more after the
last stage, and `release_staged` when the document is over. With any other backend nothing is
registered and these calls are no-ops. Documents running in parallel have different store
roots, hence different working copies and different instances.

### 4. Tests

`tests/enterprise_pdf_rag/object_backend/test_staged_backend.py` (whole-file atomic publish,
kill before rename, resume from the published file, local progress kept and republished,
a replaced published file wins, publisher lease, registry sharing, model cache on files,
default never staged); the ADR 0036 conformance pack runs on `files` / `sqlite` / `staged`;
`tests/enterprise_pdf_rag/adapters/test_staged_store_wiring.py` pins `FULL_STORE_DIGEST` /
`FULL_PUBLISHED_ID` under staged, one whole `store.sqlite` per store root, five commits per
document, and a rerun after the local disk was wiped: restored, zero model calls, same id.

## Weaker / unverified

- **Nothing here is measured on Databricks.** A commit copies the whole db each stage; for a
  300-page document the db is tens of MB, so five sequential writes of it may or may not beat
  thousands of small FUSE operations — that is the measurement the decision waits for.
- A crash loses the local progress of the current stage only if the local disk is lost too
  (new cluster); then the run resumes from the last published stage (model calls replayed
  from the model cache, which is not staged).
- Readers in another process (the API) see the version published at the moment they first
  opened the store root; an instance does not refresh while it lives. Staged is meant for the
  ingest writer.
- Switching an existing plain-`sqlite` store root to staged copies only `store.sqlite`; a
  non-empty `-wal` beside it (a writer that did not close) is not merged. ADR 0036's `close()`
  checkpoints, so a cleanly closed db has none.
- Two instances for the same root in one process (constructed directly, outside the registry)
  share the process-scoped publisher lease; the registry is the supported path.

## Rejected alternatives

- **sqlite directly on FUSE** (ADR 0036 `sqlite`): unverified locks / `-shm`; random page
  writes are the worst access pattern for a network mount.
- **rsync-style per-file publish of the file layout**: keeps tens of thousands of files on FUSE.
- **Commit on every page**: whole-file copies per page cost more than they save; stage
  boundaries are where a resume needs a consistent point.
- **Staging the model cache**: shared by processes; would need merge logic, not a copy.

## Amendment 1 (2026-10-09, [ADR 0046](0046-four-files-per-document.md)): the document's model cache is staged

§3's reason for keeping the model cache on files — "the root-level answer cache is written by
several processes" — holds for `<ingestion root>/model-cache` only. The ingest model cache is
`<document>/processing/model-cache`: one per document, written only by the process that holds
that document's publisher lease. In staged mode it is now a `StagedModelCacheBackend` (local
sqlite working copy, published whole with the stores at every stage boundary as
`processing/model-cache/model-cache.sqlite`); the root-level answer cache stays on files. The
"rejected: staging the model cache" alternative below is narrowed accordingly. A crash with the
local disk lost now resumes from the model cache as last published (its stage boundary), not
from a per-call file layout. Staged object stores also inline up to 8 MiB by default, the
`atexit` hook finishes (closes and releases) instead of only committing, and the publisher claim
is taken under a lock (page threads, ADR 0045). Result: four files per document on the mount.
