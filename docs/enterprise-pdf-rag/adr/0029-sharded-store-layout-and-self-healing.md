# ADR 0029: Sharded store directories, read-through of the flat layout, and self-healing writes

Status: Accepted, 2026-10-06. Amends the on-disk layout of the local stores behind
[ADR 0001](0001-architecture.md) (content-addressed immutable snapshots) and
[ADR 0010](0010-generic-pdf-ingestion-entry.md) (the ingestion directory), and revises, within a
stated scope, the "a corrupted existing object is refused, never repaired" rule of
[ADR 0020](0020-storage-without-hard-links.md) and [ADR 0024](0024-source-verification-cache.md).
It changes **no hash, no fingerprint, no artifact byte, no request fingerprint and no published
id** — only where two kinds of file live. An ingestion directory written before this ADR is read,
reused, published and answered from without migration.

## Context

A user's `run_folder_pipeline()` on Databricks serverless (project root on Workspace files)
failed ingesting a report of a few hundred pages:

```
AsyncFlushFailedException: One or more writes may have failed when writing to Databricks Workspace.
... MAX_CHILD_NODE_SIZE_EXCEEDED: Size limit exceeded for folder
'.../data/ingestion/feeb5739…/processing/objects/sha256' ... Current size: 10000 Limit: 10000
```

Two facts follow.

1. **A Workspace-files folder holds at most 10 000 children.** Every content-addressed object of
   a store lived flat in `objects/sha256/<digest>` and every stage-cache pointer flat in
   `stage-cache/<fingerprint>`. Measured on the lite folder run (7-page synthetic report, 14
   objects, 10 model calls; linear at 35 pages): one cached stage costs **two** files — the
   pointer and the `StageEnvelope` object it names — plus its output. A lite ingest writes
   S ≈ 2P + Σ k·M_kind + E stage-cache pointers (P pages: canonical + partition; per object of
   each kind k = Text 5, Table 7, Chart 12, Diagram 12, Formula 10, Image 2; E = one embedding
   per indexed member) and ≈ 2S + P processing objects (envelope + output per entry, page
   metadata). Measured: 119 pointers against 122 predicted (identical inputs dedupe), 248
   objects. For 300 pages and 3 000 objects (≈ 6.5 entries per object on a text-heavy report,
   6.6 on the synthetic mix) that is ≈ 20 000–23 000 pointers and ≈ 40 000–47 000 objects: the
   flat directories overflow at roughly a fifth of the document.
2. **Writes are flushed asynchronously** (already recorded in `databricks-deployment.md`): an
   error can surface after `write` / `close` / `rename` returned, so the last files written before
   the failure may be missing, empty or truncated although the writer saw success — and the
   ADR 0020 read-back after rename may well have been served from the page cache.

Before this ADR a damaged object was refused on every path, including a `put` that held the very
bytes its name means, and a damaged stage-cache entry or model-cache response raised forever: one
lost file turned into a document that could never be ingested again without a person deleting
files by hand.

## Decision

### 1. Sharded layout, written only there

| Flat (legacy, read only) | Sharded (written) |
|---|---|
| `<store>/objects/sha256/<digest>` | `<store>/objects/sha256-sharded/<digest[:2]>/<digest>` |
| `<store>/stage-cache/<fingerprint>` | `<store>/stage-cache-sharded/<fingerprint[:2]>/<fingerprint>` |

Both stores of a document (`source/`, which also holds the source stage-cache, and
`processing/`) use it. One level of two hex digits: 256 shard directories (far below the limit as
children of their parent), and a store can hold about 256 × 10 000 ≈ 2.5 M files of each kind before
any shard is at the limit — at a 300-page report's ≈ 47 000 objects a shard holds ≈ 185 on
average (binomial spread ±14), a 50-fold margin. Two levels would create almost one directory per
object (65 536 possible shards for tens of thousands of objects): a `mkdir` network round trip per
write for nothing.

The sharded root is a **new sibling** (`sha256-sharded`, `stage-cache-sharded`), not a subfolder of
the flat directory: in the field the flat `objects/sha256` already had 10 000 children, so even
creating `objects/sha256/ab/` would have been refused. Nothing new is ever written into a flat
directory, so a full one is harmless and **no migration is needed**.

The helpers live in `ragspine.common.evidence.file_placement` (`sharded_path`, `stored_path`,
`read_stored`, `stored_names`), beside `link_new_file`, which still places every file (ADR 0020).

### 2. Read compatibility

- **Lookup order: sharded first, then flat.** New and re-ingested data is all sharded, so the
  common case costs one open; an old directory costs one failed open more per read. Reads use
  EAFP (`read_bytes` on the sharded path, then the flat one), so a sharded hit is one call.
- **Both present** (only after a repair, see 3, or a partial migration): the sharded copy is the
  one read; both carry the same name, and a name is a digest, so they can only differ if one is
  damaged — and the damaged one is the one a write would have replaced.
- **Neither present**: `FileNotFoundError`, as before.
- `put` considers both places when deciding an object already exists, so an object in the flat
  layout is verified and **not written again**.
- Enumeration covers both: `LocalDocumentStore.digests()`, `stored_names(flat)`. No production
  path enumerates these directories (`scan_catalog` lists only the ingestion root's document
  directories); the tests that did now use these helpers.
- Pointers (`current-manifest`, `current-processing`), `runs/`, `document-tree/` and the model
  cache are **not** sharded (see 6).

### 3. Self-healing writes, refused reads

The rule "corrupted evidence is refused, never repaired" protects one thing: **bytes that do not
hash to their name never reach a proof, a prompt or an answer.** That is unchanged — every read
(`get`, `read_content`, `load`, `verify`, the mount, the drift guard) still refuses a missing or
mismatching object. What changes is what a **write** does when it finds one:

- **`LocalDocumentStore.put(data)`**: an existing object (either layout) whose bytes are missing,
  empty, truncated or of another digest is rewritten in the sharded layout with `data` — by
  construction the bytes its name means, so this restores the object and cannot tamper with it
  (a tamperer would need a SHA-256 preimage). An intact object is still never rewritten. The
  rewrite is fsynced temporary + `os.replace` + read-back (`replace_file`).
- **`ProcessingStore.cached(fp)`**: an entry whose pointer is unreadable or not a digest, or whose
  envelope or output object is missing or damaged, is a **miss**, not an error. The stage is
  recomputed; its model calls replay from the model cache, so recomputing costs no live call
  unless the model cache was damaged too. `cache()` then replaces the damaged pointer instead of
  reporting `Conflicting immutable stage cache entry`. A pointer whose envelope is intact and
  names a *different* outcome is still a conflict (`Stage fingerprint already names another
  actual output`), and an envelope bound to another fingerprint is still refused.
- **The source stage**: a cached source snapshot whose page SVG / text or PDF object is damaged
  is re-extracted from the PDF; extraction is deterministic, so the same bytes go back under the
  same digests and the manifest id does not change.
- **The model cache** (`JsonCompletionClient`): a record that does not parse, or a success record
  whose response is missing or not its digest, is sent again **once**, under the usual claim and
  budget (never with `cache_only`, which still raises `invalid_cache_record` /
  `missing_cached_response` / `cached_response_digest_mismatch`). The response is written with
  replace (content-addressed: a file of that name with other bytes is damaged, never a rival); the
  record is replaced only when the new response differs from the one it named. A repair that fails
  (transport error, budget) writes no failure record, so the next run tries again. A record bound
  to another fingerprint is still `cache_binding_mismatch`. After ADR 0023 the record — not the
  claim — answers, so a lost response no longer blocks the request forever.
- **A lost `current-processing` pointer or release** makes the run rebuild the index and publish
  again instead of failing (`ProcessingStore.current_id()` is `None` for an unreadable pointer).

Every repair is counted by kind (`object`, `stage_cache`, `source`, `model_cache`) through a
context-variable ledger (`recording_repairs` / `note_repair`): `run_folder_pipeline` scopes one
ledger per document, `DocumentRun.storage_repairs` keeps it, the `document_done` event carries the
total and `notebooks/run_folder.ipynb` shows a `storage_repairs` column and a note.
`JsonCompletionClient.repaired_count` counts its own. Counts only, never content.

### 4. No delayed re-verification pass

A re-read right after writing would not detect an asynchronous flush failure: the bytes come back
from the page cache of the process that wrote them (that is exactly why ADR 0020's read-back passed
in the field). The failure shows up either as an error on a later operation of the same run —
which fails that document — or on the next run's reads, which section 3 now repairs. The existing
once-per-stage verification (ADR 0024) is kept as it is.

### 5. Measured

`tests/enterprise_pdf_rag/adapters/test_sharded_layout_pipeline.py` on the 7-page mixed report:

- a store turned back into the flat layout reruns with **0** model calls, the same published id,
  no file added to any flat directory and no sharded directory created;
- with every flat directory refusing writes (`MAX_CHILD_NODE_SIZE_EXCEEDED`, `EIO`), a
  partly-ingested document finishes and publishes the same id the full-mode regression pins;
- damaging every ninth file (49 objects / pointers cut in half, 2 responses deleted) and rerunning:
  2 live calls (one per lost response), `storage_repairs = {object: 33, stage_cache: 31,
  model_cache: 2}`, the same published id; a third run 0 calls and 0 repairs.

Across versions (scratchpad harness, lite folder run with question answering, once with hard
links and once with `os.link` refusing `EPERM`): a store written by the previous release
(`b8243bc`, flat layout) reruns under this code with 0 model calls, no file added, the same
published id and every question answered; a second PDF then writes only sharded files
(223 files, 0 in flat directories); with the four flat directories refusing every write and 52 of
their files emptied or truncated, the next run repairs `{object: 42, stage_cache: 37, source: 1}`
into the sharded layout with 0 model calls, the same published id, and answers again.

The full-mode byte-for-byte regression (`FULL_STORE_DIGEST`, 815 files) still matches when each
sharded path is mapped back to its logical flat name — same names, same bytes — and the layout
itself is pinned by `sharded_layout_only`.

### 6. What is not sharded, and why

- **The model cache** (`model-cache/{requests,responses,contexts}/`): one record, one context and
  at most one response per call — 3C files for C calls (≈ 1 800 for 600 calls), far from the limit
  for one document; the shared answer cache grows by about three files per question. Sharding it
  would also move the `.claim` files that serialise calls across processes (ADR 0023), and a claim
  left in the flat directory by an older process would not be seen. Left flat; revisit when a
  per-document cache approaches a few thousand calls.
- **Pointers, `runs/`, `document-tree/`, `source/pages/`**: bounded by documents, runs or pages.

### 7. Migration

None is needed, and none runs automatically. A full flat directory keeps working because it is
only read. Moving its files into the sharded layout would only save the extra failed open per
legacy read, at one network round trip per file on Workspace files (≈ 10 000 renames for the field
directory, roughly 5–10 minutes at 30–60 ms each).

## Consequences

- Reads of legacy data cost one extra failed open per object; new data costs nothing extra.
- One more directory level, and up to 2 × 256 shard directories per store.
- A damaged entry costs, on the next run, a recomputation (model cache replay) or, for a lost
  response, one live call — instead of a document that can never be ingested again.
- The guarantee "a byte that does not hash to its name never reaches a proof, a prompt or an
  answer" is unchanged.

## Rejected alternatives

- **Sharding inside the flat directory** (`objects/sha256/ab/…`): the field's directory was already
  full, so creating the first shard would fail.
- **Two levels of shards**: ~one `mkdir` per object for headroom no document needs.
- **Treating a damaged object as absent on read** and letting the caller rebuild it: a read cannot
  tell a lost file from a tampered one, and most reads have no bytes to rebuild from; only a writer
  holding the content can repair, and only it does.
- **A post-write verification pass**: served from the writer's page cache, it cannot see an
  asynchronous flush failure.

## Amendment 1 (2026-10-06): the envelope travels inside its stage-cache pointer

Every cached stage cost three files: the pointer `stage-cache-sharded/<ab>/<fingerprint>`, the
`StageEnvelope` object it named, and the stage's output. The envelope is a few hundred bytes that
only the pointer ever reads, so it now lives **in** the pointer, and one file per cached stage
disappears. Changed: `ProcessingStore.cache` / `_lookup` / `_write_pointer` only (both the source
and the processing store use them).

**Format.** A new pointer is two lines — the envelope's SHA-256 digest (exactly the name its
envelope object used to have), then the envelope bytes themselves
(`StageEnvelope(outcome=…).model_dump_json()`, unchanged):

```
<sha256 of the envelope>\n
{"outcome":{...}}\n
```

**Reading both formats.** The first line is always the envelope's digest; what follows decides:

| After the first line | Format | The envelope is |
|---|---|---|
| nothing (or whitespace) | legacy (every release before this amendment) | the store object of that digest, read and re-hashed as before |
| the envelope | inline | those bytes, which must hash to the first line |

Lookup uses `read_stored` (sharded, then flat) directly, one call fewer than the earlier
`stored_path` + read.

**Unchanged.**

- No stage fingerprint, no envelope byte, no artifact, no processing id and no published id moves:
  the full-mode regression's 535 non-pointer files are byte-identical, its 140 envelope objects
  are gone and its 140 pointers carry them (`FULL_STORE_DIGEST` re-recorded `ddade1cd…` / 815 files
  → `620f220d…` / 675 files; `test_inline_stage_cache` turns the pointers back into the legacy form
  and gets `ddade1cd…` / 815 again). `FULL_PUBLISHED_ID` is unchanged.
- **Self-healing (section 3) is equivalent.** An inline pointer that is empty, cut short, has
  other bytes than its digest names, or does not start with a digest is a miss (counted once as
  `stage_cache`), recomputed and replaced — the digest line keeps the guarantee that envelope
  bytes which do not hash to their name are never used. A legacy pointer whose envelope object is
  lost or damaged is a miss too, and is replaced by an inline one. An intact entry naming another
  outcome is still `Stage fingerprint already names another actual output`; an envelope bound to
  another fingerprint is still refused.
- **First writer wins** (`immutable=True`, hard link or the ADR 0020 rename fallback): an existing
  pointer is accepted only when its first line is the same digest — a concurrent writer of the
  same entry, in either format, is not a conflict; any other is `Conflicting immutable stage cache
  entry` and the first writer's bytes stay.
- An intact legacy pointer is **never rewritten**: an old ingestion directory keeps its pointers
  and envelope objects, hits, publishes and answers with no migration; only new or repaired
  entries are inline, so one store can hold both formats.
- ADR 0024's verification cache is untouched (the artifact is still re-read by `get` on a hit).
- Nothing in production enumerates the stage cache (`scan_catalog` lists document directories,
  review exports read manifests); the test helpers that read pointers handle both formats.

**Measured** (scratchpad harness, offline stub sender; synthetic mixed report, lite unless
noted; file-system operations counted under the ingestion root):

| Run | Files before → after | Envelope objects | I/O operations before → after |
|---|---|---|---|
| lite, 7 pages, 14 objects | 418 → 298 (−29 %) | 120 → 0 | 5 979 → 4 773 (−20 %) |
| lite, 35 pages, 70 objects | 1 978 → 1 382 (−30 %) | 596 → 0 | 26 816 → 21 406 (−20 %) |
| full, 7 pages, with tree | 843 → 702 (−17 %) | 141 → 0 | 8 337 → 6 950 (−17 %) |
| lite 35 pages, rerun, all hits | — | — | 11 757 → 10 030 (−15 %) |

Stage-cache files (pointers + envelopes + outputs) fall to two thirds: 354 → 234 at 7 pages,
1 730 → 1 134 at 35. Each new entry saves one object write (temporary file, link, unlink, the
existence checks and its shard `mkdir`); each hit reads two files instead of three.

**The per-kind formula becomes:** a lite ingest writes S ≈ 2P + Σ k·M_kind + E stage-cache
pointers (unchanged) and ≈ S + P processing objects (was ≈ 2S + P; measured 129 objects for
S = 119 at 7 pages and 593 for S = 595 at 35, identical outputs deduping). For 300 pages and
3 000 objects that is ≈ 20 000–23 000 pointers and ≈ 20 000–24 000 objects (was 40 000–47 000),
plus ≈ 600 source objects and 3 files per model call: **≈ 43 000–50 000 files instead of
63 000–73 000, about 20 000–23 000 fewer (a third)**, and ≈ 94 objects per shard instead of 185.

**Across versions** (scratchpad harness, lite folder run answering 5 questions, once with hard
links and once with `os.link` refusing `EPERM`): stores written by `b8243bc` (flat), `2825c57`
(sharded) and `b989625` (the last commit before this amendment) rerun under this code with
0 model calls, 0 repairs and 5/5 answered. From `b989625` nothing is written and the published id
is the same; from `2825c57` / `b8243bc` both land on one new id, and from `2825c57` unmodified
`b989625` lands on that same id (one embedding stage changed between those releases — 6 new
files there, 5 here, the envelope being the difference). A
second PDF then writes 65 inline pointers and **no envelope object**; damaging 40 recent files
repairs `{object: 25, stage_cache: 18}` into the inline format with 0 calls and the same ids.

**Rejected:** dropping the digest line and trusting JSON parsing alone — a pointer whose
`producer` or artifact field changed by a byte would still parse, be used, and change a
processing id; the digest line keeps "bytes that do not hash to their name are never used", and
makes the legacy pointer a prefix of the new one.

## Amendment 2 (2026-10-06): a small stage output travels inside its pointer too

After Amendment 1 a cached stage still cost two files: the pointer (digest line + envelope) and
the stage's output object. The output is usually a few KiB and is named by its digest only, so a
small one now lives **in** the pointer, after the envelope, and the output object disappears.
Changed: `ProcessingStore.cache_output` (new; `cache` is unchanged), `_lookup`, `_write_pointer`,
and the read path of `LocalDocumentStore`; the stage writers that used `assets.put` + `cache`
(`aia_processing` canonical / partition / normalized_partition, `object_processing`,
`semantic_objects`, `page_metadata_extraction`, the two embedding writers of
`processing_retrieval`) call `cache_output` instead. Not changed: the source stage (its output is
the source manifest, addressed by its id from `current-manifest` and every processing manifest),
the document-tree stage (its output is written whatever the stage state and named by the
`document-tree/` record), and every object that is not a stage output (manifests, plans, indexes,
deterministic page metadata, review exports).

**Format.** A pointer written by `cache_output` for an output of 1 to `INLINE_ARTIFACT_LIMIT`
(64 KiB) bytes is the Amendment 1 pointer followed by the output's **raw bytes**:

```
<sha256 of the envelope>\n
{"outcome":{... "artifact":{"sha256": D, "byte_length": N, ...}}}\n
<exactly N bytes whose SHA-256 is D>
```

The envelope is compact JSON and holds no raw newline, so everything after the second newline is
the output; it must be exactly `byte_length` bytes hashing to the envelope's `artifact.sha256`.
Raw bytes rather than base64: nothing has to be escaped (the length and digest delimit them), the
file is a third smaller, and `link_new_file`'s read-back comparison (the EPERM fallback) compares
the very bytes written. A larger or empty output is put as an object and cached exactly as in
Amendment 1. Digests are unchanged by construction: the output's `AssetRef` is computed from the
same bytes the object would have held.

| After the digest line | Generation | The output is |
|---|---|---|
| nothing | before Amendment 1 | an object; the envelope an object too |
| envelope + `\n` | Amendment 1, or a large / empty output | an object |
| envelope + `\n` + N bytes | Amendment 2 | those bytes |

**Threshold, measured** (every stage-cache entry of the synthetic mixed report, offline stub
sender): 7-page lite 120 entries, p50 1.3 KiB, p90 6.9 KiB, max 10.0 KiB; 35-page lite 596
entries, p50 1.2 KiB, p90 7.0 KiB, max 10.0 KiB apart from the source manifest (25.8 KiB, which
grows with the page count and is never inlined); 7-page full 141 entries, max 10.0 KiB. Largest
by stage: svg ≤ 10.0 KiB, native_crop ≤ 8.1, table_detection ≤ 6.5, ir ≤ 6.3, canonical ≤ 4.7,
partition ≤ 3.4, qualification ≤ 2.9, embedding ≤ 1.0 (offline 64-dimension embedder). On a real
report the outliers are dense page SVG / canonical text and real embeddings (1 024 to 3 072
floats ≈ 20 to 60 KiB of JSON). 64 KiB covers all of these while keeping a pointer small enough
to be read whole on every hit; anything larger keeps its own object.

**One resolution layer for reads by digest.** Every path that reads an output by its digest —
`get`, `read_content`, `verify`, `content_path` / `asset_path`, so `load`, `save_draft`, the
retrieval plan / index / embedding reads, the mount and its drift guard, the source-review
reads — goes through `LocalDocumentStore._read_digest`:

1. a **known inline location** (a process-wide index, per store root, of digest → pointer name,
   filled by every `cache_output` and every stage-cache hit of this process);
2. the **sharded object**, then the **flat object** (ADR 0029 section 2);
3. a **scan** of the store's `stage-cache-sharded/` for pointers no scan has settled yet, which
   indexes every intact inline output (and marks it verified for that store instance, ADR 0024),
   then step 1 again. Neither: `FileNotFoundError`; a located copy whose bytes do not hash:
   `ValueError` (digest mismatch), as for an object.

A location is only where to look: the bytes found there are hashed against the digest asked for,
so the guarantee "bytes that do not hash to their name never reach a proof, a prompt or an
answer" is unchanged. The scan exists because not every reference carries a stage fingerprint (a
retrieval plan names its members' embeddings by `AssetRef` only) and must not change; it runs
only in a process that reads an inline output it has not met, typically once per mount or per
rerun of a published document (measured: 0 scans in a fresh ingest, 1 in a 35-page rerun,
≈ 230 shard listings), and its reads stand in for the verification reads that would follow.
Intact pointers are never read twice by later scans. `digests()` still lists object files only;
`ProcessingStore._lookup` reads an entry that names an object from the object files only
(`get_object` / `read_object`): a copy inline elsewhere does not make such an entry whole.

**Unchanged.**

- No stage fingerprint, envelope byte, output byte, processing id, published id or request
  fingerprint moves. The full-mode regression loses 132 output objects into its pointers:
  `FULL_STORE_DIGEST` is re-recorded `620f220d…` / 675 files → `363650ac…` / 543 files;
  `test_inline_stage_artifacts` writes the inline outputs back as objects and gets `620f220d…` /
  675 again, and `test_inline_stage_cache` turns that into the pre-Amendment-1 store and gets
  `ddade1cd…` / 815. `FULL_PUBLISHED_ID` and `FULL_REQUESTS_DIGEST` are unchanged. The store
  holds fewer files, so `test_sharded_layout_pipeline` (section 5) now damages every sixth file
  instead of every ninth to keep more than 20 sharded files damaged, and its legacy-flat
  emulation writes inline outputs back as objects first, as every flat-layout release stored
  them.
- **Self-healing (section 3) is equivalent.** An inline output that is cut off, truncated, of
  other bytes or followed by extra bytes, a pointer cut inside its envelope, an empty pointer and
  one not starting with a digest are all a miss (counted once as `stage_cache`), recomputed and
  replaced. A pointer cut exactly after its envelope reads as an Amendment 1 entry whose output
  object is missing — also a miss. An intact entry naming another outcome is still `Stage
  fingerprint already names another actual output`; an envelope bound to another fingerprint is
  still refused.
- **First writer wins** (hard link or the ADR 0020 rename fallback): only the first line is
  compared, so a concurrent writer of the same entry in any of the three formats is not a
  conflict, and any other is `Conflicting immutable stage cache entry` with the first writer's
  bytes kept.
- Earlier pointers are **never rewritten**: a digest-only or Amendment 1 entry whose output object
  is intact hits, publishes and answers as it is; only new or repaired entries carry their
  output, so one store can hold all three generations.
- ADR 0024's verification cache is untouched: an inline output read and hashed (a hit, a `get`, a
  scan) is recorded like an object read, one written is not.

**Duplicates.** An object was stored once per store whatever wrote it; an inline output is stored
once per pointer, so identical outputs of two entries of one document are now two copies (there
was never sharing across documents: each document has its own store). Measured: 7-page lite
6 duplicate copies, 36.5 KB (store 751.5 → 788.1 KB, +4.9 %); 35-page lite 58 copies, 246 KB
(3.58 → 3.83 MB, +6.9 %); 7-page full 7 copies, 41 KB (2.257 → 2.298 MB, +1.8 %). They are almost
all an object's `native_crop` equal to its `svg`, plus repeated `model_render` /
`table_detection` / `qualification_exclusions`. Accepted: a few per cent of bytes against
thousands of files and network round trips — on Workspace files the cost is per file and per
operation, not per byte — and deduplicating them would bring back the shared object file this
amendment removes.

**Measured** (scratchpad harness, offline stub sender, synthetic mixed report; file-system
operations counted under the ingestion root):

| Run | Files A1 → A2 | Processing objects | I/O operations A1 → A2 |
|---|---|---|---|
| lite, 7 pages, 14 objects | 298 → 185 (−38 %) | 129 → 16 | 4 773 → 3 592 (−25 %) |
| lite, 35 pages, 70 objects | 1 382 → 845 (−39 %) | 593 → 56 | 21 406 → 16 223 (−24 %) |
| full, 7 pages, with tree | 702 → 570 (−19 %) | 142 → 10 | 6 950 → 5 574 (−20 %) |
| lite 35 pages, rerun, all hits | — | — | 10 030 → 8 634 (−14 %) |

Against the release before Amendment 1 the 7-page lite run is 418 → 185 files (−56 %). Each new
entry saves the output's object write (temporary file, link, unlink, existence checks, a shard
`mkdir`); each hit reads one file instead of two.

**The per-kind formula becomes:** a lite ingest writes S ≈ 2P + Σ k·M_kind + E stage-cache
pointers (unchanged) and ≈ 1.5 P processing objects that are not stage outputs (deterministic
page metadata, unscored members, plan, index, manifests; measured 16 at 7 pages and 56 at 35;
was ≈ S + P). For 300 pages and 3 000 objects: ≈ 20 000–23 000 pointers, ≈ 450 processing
objects, ≈ 600 source objects and 3 files per model call — **≈ 23 000–26 000 files instead of
43 000–50 000** (63 000–73 000 before Amendment 1), ≈ 80–90 pointers and ≈ 2 objects per shard,
and at the measured ≈ 464 instead of ≈ 612 operations per page about 45 000 fewer file-system
operations per ingest.

**Across versions** (scratchpad harness, lite folder run answering 5 questions, once with hard
links and once with `os.link` refusing `EPERM`): stores written by `3414e0c` (Amendment 1),
`b989625` and `2825c57` (digest-only pointers) rerun under this code with 0 model calls,
0 repairs and 5/5 answered, all on the same published id (`3414e0c` / `b989625`: nothing
written; `2825c57`: the one embedding stage that changed between those releases is written, now
as 1 inline pointer and 3 other files, 4 files instead of Amendment 1's 5). A second PDF then
writes 64 inline pointers, 1 Amendment 1 pointer (the source stage) and **no output object** in
its processing store; damaging 40 recent files and deleting a response repairs `{stage_cache:
30, object: 10, model_cache: 1}` with 1 live call and the same ids. Going back: `3414e0c` run on
such a store reads each inline pointer as damaged (its envelope check covers the trailing
output), recomputes those 64 entries from the model cache with 0 calls and the same ids, and
this code then reruns that store writing nothing.

**Rejected.**

- *Base64 or a length-prefixed frame*: the envelope already states length and digest; base64
  costs a third more bytes and an encode on every write and decode on every hit.
- *An index file (digest → pointer)*: one more mutable file per store, exposed to the same
  asynchronous flush losses, and a second source of truth; the scan rebuilds the same map from
  the pointers themselves.
- *Recording the pointer's fingerprint next to each reference*: it would change the bytes of
  manifests and plans, and so every processing and published id.
- *Inlining only outputs whose readers know the fingerprint*: embeddings — read from the
  retrieval plan by `AssetRef` alone — are one entry in nine, and the scan costs one pass per
  process instead.
