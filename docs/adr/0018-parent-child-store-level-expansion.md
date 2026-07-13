---
status: accepted
date: 2026-07-14
---

# ADR 0018 — Parent-child (small-to-big) store-level expansion (批次 2.2 follow-up)

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Completes the follow-up left open by [0016](0016-retrieval-productization-config.md) (§Alternatives:
*"Repoint parent-child at retrieval-time store expansion now — deferred"*). Constrained by
[0005](0005-lean-core-experimental-isolation.md) (new capability isolated from the byte-identical
default path) and [0010](0010-intent-parser-security-decoupling.md) (deterministic isolation
independent of any model output).

## Context

ADR 0016 ③ landed the `ParentChildChunker` preset: at chunk time a child carries `window_text` (the
parent section's full text) + `parent_locator` (the section's real para span). But the chunker seam was
**not wired into narrative ingest**, and `window_text` / `parent_locator` were **not persisted** — so the
end-to-end small-to-big effect ("hit a fine child → return the parent context for generation") never fired
on the real path. This ADR closes that gap: persist the fields, wire the chunker into both ingest paths,
and expand the parent window at the retrieval exit — while keeping the default path byte-identical and the
three code-level invariants (anti-fabrication, provenance, RESTRICTED double-exit) intact.

## Decision

Three additive changes, all opt-in, default path byte-identical:

- **Persistence — additive columns, old DBs readable.** `narrative_chunk` gains four columns
  `parent_id` / `heading` / `window_text` / `parent_locator` (all `TEXT NOT NULL DEFAULT ''`).
  `ChunkStore.init_schema` runs an **additive migration** (`PRAGMA table_info` → per-column
  `ALTER TABLE ADD COLUMN`) so a pre-existing DB gains the columns with `''` defaults and every old row
  stays readable — never a destructive rebuild. `StoredChunk` mirrors the four fields (defaults `''`).
  Rejected: a schema version bump / rebuild — the additive path is simpler and keeps old libraries live.

- **Ingest wires the `Chunker` seam (opt-in).** Both writers accept a `chunker: Chunker | None` (default
  `None` → built-in `chunk_document`, **byte-identical**): `ingest_narrative(..., chunker=)` (batch pipeline)
  and `NarrativeIndex(chunker=)` (retrieval-side ingest + embed). Config surface: `ServiceConfig.chunker`
  (`make_chunker` selector, default `"none"`), threaded through the narrative-ingest worker job payload.
  When set to `parent_child` / `small_to_big`, children land with `window_text` / `parent_locator` and the
  store persists them.

- **Retrieval expands the parent window at the A-line exit (generation-context only).** In
  `narrative_link._to_snippet`, a chunk carrying non-empty `window_text` writes a **separate `prompt_text`
  key** (the parent section — the agent's `_snippet_text` already prefers it as generation context), while
  `text` / `source_locator` / `chunk_id` stay the **hit child**. `parent_locator` is attached as a
  provenance back-reference to the parent span. The window expansion **only affects the generation context**
  — it never becomes a hit, never reorders, never adds a result. Default chunkers leave both fields empty →
  neither key is added → the snippet dict is byte-identical. This reuses the exact `prompt_text` layering W8
  compression already established.

### RESTRICTED parent window — the decision (pinned)

**`window_text` rides the child's own RESTRICTED double-exit; a RESTRICTED chunk's whole snippet — parent
window included — is dropped (整段拒绝).** `_to_snippet` is invoked **only after** the `link/` exit has
already stripped `sensitivity == RESTRICTED` chunks, so a RESTRICTED child never reaches window expansion:
its `window_text` can never leak into any `prompt_text`. This is the safest of the two candidate semantics
("剔除 window" vs "整段拒绝") — we take **整段拒绝**: the parent context is only ever surfaced through a child
snippet that itself passed both exits. Under the current doc-level sensitivity model child and parent share
a sensitivity, so "child allowed but parent section RESTRICTED" cannot even arise from ingest; the decision
pins the mechanism **defensively regardless**, and the reverse-proof
(`test_parent_child_isolation.py`) hands the exit a RESTRICTED chunk carrying a window and asserts the whole
snippet — window and all — is rejected with zero leakage.

## Alternatives considered (rejected)

- **Surface `window_text` as the snippet's `text`** (so it becomes the citation source): would make the
  expanded context masquerade as the hit evidence and break provenance honesty. `text` / `source_locator`
  stay the child; the window is generation-context only.
- **Strip the window but keep the RESTRICTED child snippet**: a partial, fragile carve-out. 整段拒绝 (drop
  the whole snippet) is the simpler, safer invariant and matches the existing exit semantics.
- **Expand the window before the RESTRICTED strip**: would open a leak path. Expansion happens strictly
  inside `_to_snippet`, downstream of the strip.
- **A new schema version + rebuild instead of additive `ALTER TABLE`**: unnecessary; additive columns keep
  old DBs readable with no migration ceremony.

## Consequences

- Small-to-big now works end-to-end on the real path: a fine child hit returns the parent section as
  generation context, with the citation still pinned to the child's real locator and the parent span carried
  as a provenance back-reference.
- The default offline path stays byte-identical (default chunker → empty fields → no new snippet keys →
  scoring/provenance unchanged; `_record_metadata` / BM25 / vector never read the new fields).
- The `SentenceWindowChunker` honesty boundary (window not persisted) is also resolved — it shares this
  store-level path.
- New/changed artifacts: `ChunkStore` (+4 columns, additive migration) + `StoredChunk`; `ingest_narrative`
  / `NarrativeIndex` `chunker=` seam; `ServiceConfig.chunker` + worker payload threading;
  `narrative_link._to_snippet` window→`prompt_text` + `parent_locator`; conformance
  `tests/conformance/test_parent_child_isolation.py` (+ store persistence/migration tests).
