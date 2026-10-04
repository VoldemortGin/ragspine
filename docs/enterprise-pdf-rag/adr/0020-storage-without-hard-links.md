# ADR 0020: The local stores fall back to rename where a filesystem cannot hard-link

Status: Accepted, 2026-10-03. Amends the write path of the local stores behind
[ADR 0001](0001-architecture.md) (content-addressed immutable snapshots) and
[ADR 0010](0010-generic-pdf-ingestion-entry.md) (the ingestion directory). It changes **no
on-disk layout, no file name and no file content**: an ingestion directory written before this
ADR is read, reused and resumed exactly as before, and on a filesystem that supports hard links
every write takes the same path, byte for byte.

## Context

Three places publish a fully written, fsynced temporary file under its final name with
`os.link(temporary, target)` — "create if absent", atomically, and the first writer wins:

| Call site | What the name means | What an existing target means |
|---|---|---|
| `adapters/document_store.py` `LocalDocumentStore.put` — `objects/sha256/<digest>` | content-addressed: the name **is** the SHA-256 of the bytes | the same bytes; re-read and digest-verified, a corrupted object is refused, never repaired |
| `adapters/processing_store.py` `_write_pointer(immutable=True)` — `stage-cache/<input fingerprint>` | first writer wins: the digest of the one outcome cached for that input | the same digest is a no-op; another digest raises `Conflicting immutable stage cache entry` |
| `ragspine/common/evidence/providers/json_completion.py` `_immutable_write` — `responses/<digest>.json`, `requests/<fingerprint>[.retry-1].json`, `contexts/<fingerprint>.json` | responses are content-addressed; records and contexts are first writer wins | equal bytes are a no-op; other bytes raise `cache_conflict` (a context reports `stored_context_mismatch`) |

The model-cache records and contexts are additionally serialized by the `.claim` file
(`O_CREAT | O_EXCL`), which is created before any live call and then made durable with a
directory fsync.

A user running `notebooks/run_folder.ipynb` on Databricks had every PDF fail at `ingest` with
`[Errno 1] Operation not permitted: '<ingestion_root>/<sha256>/source/objects/sha256/<sha256>'`:
the FUSE filesystem behind the data directory refuses `link(2)`. The project requires artifacts
under `ROOT_DIR/data`, so moving the data elsewhere is not an answer. The same failure reproduces
on a macOS exFAT volume (`ENOTSUP`). The temporary file had already been created and fsynced on
that filesystem, so exclusive creation and file fsync work there; only the link failed.

## Decision

1. **One shared placement helper**, `ragspine.common.evidence.file_placement.link_new_file`,
   replaces the three `os.link` calls. It lives in `ragspine.common` because
   `enterprise_pdf_rag` already depends on it and never the other way round. It tries
   `os.link` first; only an `OSError` whose errno says *this filesystem cannot hard-link*
   (`EPERM`, `ENOTSUP` / `EOPNOTSUPP`, `ENOSYS`, `EXDEV`) takes the fallback. `EACCES`,
   `ENOSPC`, `EROFS`, `EIO` and every other errno still raise unchanged.
2. **The fallback is check, rename, re-read.** If the target exists, raise `FileExistsError`
   exactly as `os.link` would, so every caller keeps its own existing-target branch unchanged
   (verify the digest, compare the pointer, compare the bytes). Otherwise `os.replace` the
   temporary onto the target and read the target back; if it no longer holds this writer's
   bytes, a concurrent writer landed last, and that is reported as `FileExistsError` too. A
   reader still only ever sees a complete file, because the name appears by rename of a fully
   written, fsynced file — never by exclusive creation of the final name followed by writes,
   which would expose a half-written file to readers and leave one behind after a crash.
3. **The temporary is cleaned with `missing_ok=True`** at all three sites, since the fallback has
   already renamed it away. A failure on either path still removes the temporary.
4. **The claim's directory fsync tolerates "unsupported".**
   `file_placement.fsync_directory` skips `EINVAL`, `EPERM`, `ENOTSUP` / `EOPNOTSUPP` and
   `ENOSYS` (restricted FUSE mounts commonly answer `EINVAL`) and raises everything else.
   The mutual exclusion comes from `O_EXCL`; the directory fsync only makes the claim survive a
   power loss, so skipping it where the filesystem cannot do it weakens durability, not
   correctness. File fsyncs are unchanged: they succeeded on the failing filesystem.

## What is weaker on the fallback path

- **First-writer-wins is best effort under concurrency.** `os.link` decides atomically who was
  first. Check-then-rename does not: two writers of *different* bytes for the same name that both
  pass the existence check both rename, and the last one stays. Each re-reads after its rename, so
  the writer that was overwritten gets the caller's conflict error; the writer that landed last
  does not learn that another writer briefly published first. For content-addressed objects and
  responses this is harmless (same bytes). Model-cache records and contexts are serialized by the
  claim. A stage-cache pointer has no such guard: two processes producing *different* outcomes
  for one input fingerprint at the same moment can no longer be told apart reliably — a
  condition that is already an error, now detected on one side instead of both.
- **A pre-existing, corrupted object is still refused**, not overwritten: the existence check
  sends it to the same digest verification as before.
- **Claim durability** after a crash is not guaranteed where the directory cannot be fsynced.

## Rejected alternatives

- **Exclusive creation of the final name, then write.** Readers would see empty or partial
  pointers and records (`ValueError` / `invalid_cache_record`), and a crash would leave one that
  breaks every later resume.
- **A lock file next to each name.** Changes the on-disk layout and leaves stale locks after a
  crash.
- **Always rename.** Would change the behavior on filesystems that support hard links.
