---
covers:
  - src/ragspine/retrieval/chunking/chunker.py
verified-against: 443885a09c4377f83dcd1394d77a64962da5f0fe
---

# Chunker seam — pluggable chunking strategy

Deep dive behind the **`Chunker` (new)** row of
[`docs/prd-breadth-via-adapters.md`](../../../../docs/prd-breadth-via-adapters.md) (P0, ⭐).
The PRD is the originating spec; this is the live contract. This was a **behavior-preserving lift**:
the chunker already existed as `chunk_document`; the Protocol only makes the strategy swappable.

## The seam

`chunker.py` owns exactly one concern: **answer "how is a document's text split into retrieval
chunks."** Chunking is ⭐ quality-critical (it decides what a retriever can recall), so it is *owned*
— the seam exists to let semantic / contextual / parent-child strategies plug in as units, not to
rent the stage out.

```python
@runtime_checkable
class Chunker(Protocol):
    def chunk(self, text, meta, *, max_chars=..., overlap_chars=...) -> list[Chunk]: ...
```

Every `Chunk` carries `doc_id` (the lineage root) + `source_locator` (`prefix#para{start}-{end}`,
1-based) — so the Protocol is provenance-honest, and the conformance pack binds that for *every*
registered chunker.

## The default delegates byte-identically

`DefaultChunker.chunk(...)` is a **thin shim** that calls `chunk_document(...)` unchanged. The
`chunk_document` entry point and its signature are **preserved**, so every existing caller
(`narrative_ingest`, `lexical/retrieval`, `eval/qa_eval`) is untouched and the loop stays
deterministic. `tests/retrieval/chunking/test_chunker.py` pins the equality
(`DefaultChunker().chunk(...) == chunk_document(...)`) — zero behavior change is the headline of this
increment, not a side effect.

`"default"`, `"recursive"`, and `"structural"` are aliases for `DefaultChunker` (the paragraph-greedy
recursive/structural chunker is what the capability matrix calls "recursive/structural").

## Config selection (mirrors `make_vector_store`)

`make_chunker(spec=None, **kwargs) -> Chunker | None`:

- `None` / `"none"` (case/whitespace-insensitive) → `None` — the caller falls back to the built-in
  `chunk_document` / `DefaultChunker` default.
- `"default"` / `"recursive"` / `"structural"` → `DefaultChunker`.
- anything else → resolved via **entry-point auto-discovery** on the `ragspine.chunkers` group, so a
  third-party package registers a strategy by name with **no core PR**; an unknown name raises
  `ValueError` listing the built-in + discovered names.
- with `spec=None`, the env var `RAGSPINE_CHUNKER` supplies the spec.

Built-in names resolve through a lazy-loader registry; importing `chunker.py` pulls zero SDKs.

## Conformance

`tests/conformance/test_chunker_provenance.py` parametrizes over every registered chunker
(`conftest.CHUNKER_IMPLS`) and asserts each emitted `Chunk` carries a non-null `doc_id` +
`source_locator` through a single assertion core; a deliberately lineage-dropping stub chunker fed the
same core **must fail**, proving the pack is non-vacuous (the same "honest reverse proof" used by the
`VectorStore` and `SourceConnector` packs). A new chunker inherits the whole pack by adding one line
to `CHUNKER_IMPLS`.

## What this is not

The **strategies themselves** (semantic, contextual, parent-child) are the open P1 work. This
increment ships only the seam + the existing default behind it.
