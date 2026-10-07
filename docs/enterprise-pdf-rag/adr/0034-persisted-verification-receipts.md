# ADR 0034: A full verification is persisted as a receipt and reused across processes

Status: Accepted, 2026-10-06. Amends the read path of
[ADR 0024](0024-source-verification-cache.md) (a store instance verifies a content-addressed
snapshot once) and builds on [ADR 0029](0029-sharded-store-layout-and-self-healing.md) (sharded
layout, self-healing writes) and [ADR 0020](0020-storage-without-hard-links.md) (stores without
hard links), and records the inline stage outputs of ADR 0029 Amendment 2 where they live. It
changes **no object, no fingerprint, no stage-cache entry, no request
fingerprint and no published id**: it adds one small file per verified snapshot, beside the
objects. An ingestion directory written before this ADR has no receipts and is read exactly as
under ADR 0024 until a reader records them.

## Context

ADR 0024 made reads grow with pages plus objects, but kept one full verification of each
snapshot per store instance, and every stage, every catalog scan entry and every mount opens its
own instance. A full verification of a source snapshot reads and hashes the manifest, the whole
PDF and every page's SVG and text (2P + 2 files); `ProcessingStore.load` does the same for every
processing asset its manifest names. On Databricks Workspace files every one of those reads is a
network round trip, and a fully cached rerun still pays them stage after stage, for a snapshot
that has not changed since the last run verified it.

## Decision

### 1. The receipt

After a store instance has **statted, read and hashed every file of one immutable snapshot
itself**, it records a receipt (`adapters/verification_receipt.py`):

- **Where:** `<store root>/verification-receipts/<subject>`, where the subject is the snapshot's
  manifest digest (the source manifest id, or the processing id). Not under `objects/`, not
  content-addressed, never read by anything but the receipt check; per store there are as many
  receipts as manifests, so the directory is not sharded.
- **Format:** the SHA-256 of the body on the first line, then the body: canonical JSON with
  `receipt = "verification-receipt-v1"`, `subject`, `refs` = the SHA-256 of the sorted distinct
  `<digest>:<length>` pairs it covers, and `files` = one entry per distinct digest:
  `sha256`, `byte_length`, `path` (relative to the store root: the file its bytes were read
  from — the sharded or the flat object, or the `stage-cache-sharded/<ab>/<fingerprint>` pointer
  that carries a small stage output inline, ADR 0029 Amendment 2), and that file's `size`,
  `mtime_ns`, `ctime_ns` (a pointer is larger than the output it carries).
- **The stat is taken before the read** of the same path, so a receipt can never vouch for a
  state later than the bytes it hashed: a rewrite between the stat and the read either fails the
  hash or leaves a stat that no longer matches.
- **Written** atomically (temporary, fsync, `os.replace`) after the sweep passed. A store that
  cannot take it (read-only, a file in the way) simply goes without.
- **Not written** by an instance that wrote one of the files itself (its read-back is most likely
  served from its own page cache, ADR 0029 §4), nor by a sweep that only consulted the instance's
  memory (nothing was statted), nor by the catalog scan or a mount (`record_receipts=False`: both
  stay read-only), nor when the switch is off.

### 2. Reuse

A whole-snapshot sweep (`LocalDocumentStore.verify_snapshot`: `LocalDocumentStore.load` and
`ProcessingStore.load`'s asset sweep) first drops the objects this instance has itself read back
(ADR 0024). For the rest it accepts the snapshot when, and only when,

1. the receipt file reads, its first line is the SHA-256 of its body, the policy is
   `verification-receipt-v1`;
2. its subject is this manifest id and its `refs` fingerprint is that of the reference set the
   manifest names now, and its entries are exactly those (digest, length) pairs;
3. every not-yet-verified file is still at the recorded path — the sharded object (its size is
   the reference's length), the flat object with no sharded copy beside it (reads look in the
   sharded place first), or a sharded stage-cache pointer — and `stat` gives the recorded size,
   `mtime_ns` and `ctime_ns`. A pointer that still has its stat still carries the same output;
   a copy of the same digest elsewhere (an object beside an inline output) is hashed whenever a
   reader consumes it, like every byte.

That costs one receipt read and **one `stat` per object not yet read back**, instead of one read
and one hash. The manifest object itself is always read and hashed: it is what the sweep returns,
and its bytes are what the receipt is checked against. Any failed condition is simply "no
receipt": the instance does the ADR 0024 sweep, now with stat-before-read, and records a fresh
receipt. When a receipt holds, the inline locations it names are noted in the process's inline
index (locations only, never trusted — ADR 0029 Amendment 2), so a new process that consumes
those outputs does not first scan the stage cache for them. Objects a receipt vouched for are remembered per instance and accepted only by the
other verification-only sweeps of the same snapshot (`verify(ref, receipt=True)`: the retrieval
dependency sweep of `ProcessingStore._read_retrieval`), never by `verify`, `put` or `publish`.

### 3. Each entry point

| Entry point | Decision | Why |
|---|---|---|
| `LocalDocumentStore.load` (source snapshot; every stage start, `validate_processing_source`) | receipt + stat | verification-only; every byte a proof uses is still read by `get` |
| `ProcessingStore.load` asset sweep (stage starts, `_current_mode`, `_member_pages`) | receipt + stat | verification-only; the manifest object, page metadata, retrieval plan and index are read |
| `ProcessingStore._read_retrieval` dependency sweep | accepts what this instance's receipt vouched for | a subset of the same snapshot's assets |
| catalog scan (`_inspect`) and `mount_document` | receipt + stat, **never write** | both are documented read-only; the mount's per-request drift guard is unchanged |
| `publish_draft` (the publish that moves `current-*`) | **real read**, receipts off | the release about to become current is confirmed byte for byte |
| `ProcessingStore.save_draft` explicit sweep, `LocalDocumentStore.publish`, `verify`, `put` | **real read** (ADR 0024 cache only) | the objects were just written; this is the check before they become addressable |
| `get`, `read_content`, every consuming read | always read and hash | unchanged |

### 4. Switches

- `APP_VERIFY_PERSISTED_RECEIPTS` (`Settings.verify_persisted_receipts`, default `true`);
  `false` restores ADR 0024 exactly: no receipt read or written, one full sweep per instance.
  Per store: `LocalDocumentStore(persisted_receipts=...)` / `ProcessingStore(persisted_receipts=...)`.
- `APP_VERIFY_EVERY_REQUEST=1` / `verify_every_load=True` / `auditing()` still verify every
  call and ignore receipts entirely; the AIA source-review app is unaffected.

## Threat model

What a receipt protects against, and what it does not:

- **Our own incomplete write / failed asynchronous flush (ADR 0029).** A receipt cannot prove a
  file reached storage — no read can (ADR 0029 §4). What catches the loss is the stat at reuse: a
  lost, emptied or truncated file has another size or is missing, so the receipt fails and the
  full sweep refuses the file (and the next writer repairs it). An upload that completes later and
  moves the mtime also fails the receipt: one extra full verification, the safe direction. The
  writer never records a receipt for files it wrote.
- **A repaired object (ADR 0029 self-healing).** A repair replaces the file, so its mtime / ctime
  move and the receipt fails until a reader re-verifies the repaired snapshot and records again.
- **Damaged or forged receipt.** Self-hashed, bound to subject and file set: damage is "no
  receipt". A receipt is not authenticated — whoever can write the store can forge one, exactly
  as they can rewrite objects and pointers; that is outside this model.
- **External tampering that keeps size, mtime and ctime.** Setting the mtime back is easy
  (`os.utime`), but it moves the ctime, which ordinary user-space cannot set; the check includes
  it. A rewrite that preserves all three (privileged clock manipulation, a filesystem that reports
  no usable ctime) is **not** detected by a receipt reader. This extends ADR 0024's accepted window
  ("an asset rewritten after this instance verified it, and only re-verified, is not noticed by
  that instance") from one instance to every instance until the stat moves. What still holds:
  every byte that reaches a proof, a prompt or an answer is hashed when it is read; the publish
  that moves `current-*` reads everything; `APP_VERIFY_EVERY_REQUEST=1` reads everything always;
  `APP_VERIFY_PERSISTED_RECEIPTS=false` puts back one real verification per instance.
- **FUSE metadata.** On Workspace files a stat may be served from an attribute cache, and the
  ctime / mtime may be rewritten by the asynchronous upload. Both err the safe way or not at all:
  a moved stat forces a re-verification; a filesystem whose ctime is constant only loses the ctime
  half of the check.

## Measured effect

Harness: scratchpad `verify/count_io2.py` (derived from `shard/count_io.py` + `count_files.py`):
`run_folder_pipeline` over the 7-kind synthetic report repeated to P pages, `os.link` refused
with `EPERM` (ADR 0020 fallback), offline model stub, then the in-process `scan_catalog` +
`mount_catalog` the answering step uses; counts every read open, the bytes of each file read,
every `os.stat`, every write open under the ingestion directory. Baseline = the code this ADR
was rebased on (ADR 0029 Amendment 2, inline stage outputs). "Rerun" = the same command again
with every model reply cached (a second rerun counts exactly the same). Same published ids
before and after; a rerun writes nothing new either way (4 / 693 / 1 382 write opens: the review
exports), a first run writes 8 receipts more.

| mode | P | run | read opens before → after | MB read | `stat` | reads + stats |
|---|---|---|---|---|---|---|
| lite | 30 | first | 10 388 → 8 903 (−14 %) | 34.9 → 30.0 | 1 670 → 4 761 | 12 058 → 13 664 (+13 %) |
| lite | 30 | rerun | 6 844 → 4 083 (−40 %) | 25.6 → 17.4 (−32 %) | 331 → 2 814 | 7 175 → 6 897 (−4 %) |
| lite | 60 | first | 20 834 → 17 848 (−14 %) | 70.2 → 60.5 | 3 551 → 9 739 | 24 385 → 27 587 (+13 %) |
| lite | 60 | rerun | 13 711 → 8 166 (−40 %) | 51.6 → 35.0 (−32 %) | 457 → 5 620 | 14 168 → 13 786 (−3 %) |
| full | 30 | first | 12 847 → 11 778 (−8 %) | 42.3 → 38.7 | 2 500 → 6 415 | 15 347 → 18 193 (+19 %) |
| full | 30 | rerun | 8 063 → 5 035 (−38 %) | 29.7 → 21.2 (−28 %) | 520 → 3 222 | 8 583 → 8 257 (−4 %) |
| full | 60 | first | 25 687 → 23 544 (−8 %) | 85.3 → 78.1 | 5 215 → 13 036 | 30 902 → 36 580 (+18 %) |
| full | 60 | rerun | 16 104 → 10 039 (−38 %) | 59.6 → 42.6 (−28 %) | 813 → 6 419 | 16 917 → 16 458 (−3 %) |

Where it comes from on a lite rerun at P = 60: source-store reads 2 046 → 1 476 files and
12.2 → 7.8 MB (the whole PDF is no longer read once per stage / scan / mount); stage-cache pointer
reads 10 041 → 5 473 (the processing sweeps, whose small outputs now live inline in the pointers,
and the stage-cache scans a new process ran to find them, which the inline locations a receipt
names make unnecessary). Eleven receipts are read per rerun.

The honest reading: a receipt turns each skipped read into one `stat`. On a rerun reads fall by
38–40 %, bytes by 28–32 %, and reads + stats counted alike still fall slightly (−3 to −4 %); on a
first run, where each snapshot is first recorded (a stat before each read) and then reused by the
next stages, reads fall 8–14 % but reads + stats counted alike **rise** 13–19 %. Whether either is
faster on Databricks depends on what a `stat` costs there relative to opening, reading and closing
a whole file, which has **not been measured**. Extrapolated linearly to 300 pages (×5 of P = 60):

| 300 pages | reads + stats before → after | 20–50 ms each, counted alike | a `stat` = ⅓ of a read |
|---|---|---|---|
| lite rerun | 68.6 k + 2.3 k → 40.8 k + 28.1 k | 24–59 → 23–57 min | 23–58 → 17–42 min (−28 %) |
| full rerun | 80.5 k + 4.1 k → 50.2 k + 32.1 k | 28–71 → 27–69 min | 27–68 → 20–51 min (−26 %) |
| lite first run | 104.2 k + 17.8 k → 89.2 k + 48.7 k | 41–102 → 46–115 min | 37–92 → 35–88 min (−4 %) |

Per full source verification of a 300-page PDF: 601 reads of the PDF and every page file (the PDF
alone tens of MB for a real report) → 601 stats and one receipt read. `APP_VERIFY_PERSISTED_RECEIPTS
=false` is the way back if `stat` turns out as expensive as a read on the target mount.

The remaining reads on a rerun are consuming reads (stage-cache lookups and their outputs, page
text sidecars and SVGs every proof reads, page metadata, embeddings), which no receipt may skip.

## Rejected alternatives

- **A receipt without a stat check** (trust the receipt if intact): would make every snapshot
  file that no proof consumes unverifiable until an audit, and a lost flush invisible to every
  reader.
- **Size + mtime only.** `os.utime` restores the mtime of a same-length rewrite; the ctime is the
  part user space cannot put back.
- **Recording in the writing instance**, or recording from the instance's memory: the former is
  the read-back ADR 0029 §4 shows proves nothing; the latter has no stat to record.
- **Writing receipts from the scan / mount**: both are read-only by contract; the pipeline stages
  record them.
- **Using receipts in the final publish**: the publish that switches `current-*` is the one point
  where "these bytes are on disk now" must be read, not inferred.
- **A process-wide memo of validated receipts** (no stat on the second instance): the same reason
  ADR 0024 rejected a process-wide cache — a new instance must look at the disk.
