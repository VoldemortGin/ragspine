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
