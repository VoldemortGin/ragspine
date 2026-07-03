---
covers:
  - src/ragspine/retrieval/chunking/chunker.py
verified-against: 39de878
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
`"layout"` / `"parent_child"` / `"parent-child"` select `LayoutAwareChunker` (W4b, below);
`"sentence_window"` / `"sentence-window"` and `"semantic"` select the two W10 strategies (below).

## Layout-aware + parent-child (W4b)

`layout_chunker.py`'s `LayoutAwareChunker` is the first **non-default** strategy behind the seam —
opt-in via `make_chunker("layout")` / `RAGSPINE_CHUNKER`; the default stays `DefaultChunker` and
**byte-identical**. It splits on **structural boundaries** instead of fixed length:

- `is_heading(line)` — a deterministic heuristic: markdown `#`, numbered (`1.` / `1.2` / `一、`) or
  `第N章` headings, or a short punctuation-free line. `_sections` groups paragraphs into sections at
  those boundaries; **a chunk never merges across a section.** Each child carries `parent_id`
  (`{doc_id}#s{k}`) + `heading`; `group_children_by_parent` regroups siblings for **small-to-big**
  expansion (retrieve the small child, expand to the parent section for synthesis).
- **Within** a section it **reuses `chunk_document`** (same budget-greedy packing / overlap /
  oversized sentence-or-hard split), then **remaps the local paragraph numbers back to global** ones
  so `source_locator` / `para_start..para_end` stay citation-honest and `chunk.text` remains an
  original-substring paragraph join. Param validation + empty/whitespace handling are inherited by
  delegating the per-section call to `chunk_document`.

`Chunk` gained two **optional, default-`""`** fields — `parent_id`, `heading` — so the addition is
equality-safe (the `DefaultChunker == chunk_document` and byte-identity goldens still hold) and
backward-compatible. **Follow-up (not this increment):** consuming the *richer* structure the family
extractors expose (heading levels, table edges from pdfspine/docspine), and persisting
`parent_id`/`heading` through `chunk_store` for retrieval-time parent expansion.

## Sentence-window + semantic (W10)

Two more **non-default** strategies behind the same seam, opt-in via
`make_chunker("sentence_window")` / `make_chunker("semantic")`; the default stays `DefaultChunker`
and **byte-identical**. Both are provenance-conformant (`CHUNKER_IMPLS` now
`("default", "layout", "sentence_window", "semantic")`, so the pack runs on them) and keep the
`chunk.text` = original-substring contract.

- **`sentence_window_chunker.py`'s `SentenceWindowChunker`** (benchmarks LlamaIndex
  `SentenceWindowNodeParser`) — decouples **retrieval granularity** from **generation context**:
  one chunk *per sentence* (precise recall; `chunk.text` = the sentence, a substring), plus a
  `window_text` holding the ±`window_size`-sentence window for synthesis-time expansion. `Chunk`
  gained a third **optional, default-`""`** field — `window_text` — again equality-safe (default
  chunkers leave it `""`; the goldens hold). `source_locator` / `para_*` point at the sentence's
  paragraph (global 1-based). **Follow-up:** persisting `window_text` through `chunk_store` and
  swapping the window back in at prompt time (the same retrieval-time wiring deferred for W4b
  small-to-big); budget-splitting an oversized single sentence.
- **`semantic_chunker.py`'s `SemanticChunker`** (benchmarks LlamaIndex `SemanticSplitterNodeParser`)
  — splits on **embedding-similarity boundaries** instead of fixed length: it embeds each paragraph,
  computes consecutive-paragraph distance (`1 − cosine`), and starts a new chunk where the distance
  is a spike (`≥` the `breakpoint_percentile` of the distance spectrum **and** `> 0`, so identical
  neighbours never split). **Within** a semantic segment it **reuses `chunk_document`** (budget /
  overlap / oversized split) and **remaps local paragraph numbers back to global** — exactly the
  `LayoutAwareChunker` idiom, but the boundary comes from embedding distance rather than a heading,
  so provenance / substring / param-validation are inherited. The default embedder is the zero-dep
  deterministic **lexical-hash** `DeterministicEmbeddingBackend` (offline, byte-reproducible — so
  the conformance pack runs offline); inject the real-semantic ONNX backend (`[embed-onnx]`) for
  quality. **Follow-up:** sub-paragraph (sentence-level) semantic boundaries need sub-paragraph
  locators, which the system's paragraph-granular `source_locator` doesn't yet express.

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
(`conftest.CHUNKER_IMPLS` — now `("default", "layout", "sentence_window", "semantic")`) and asserts each emitted `Chunk` carries a
non-null `doc_id` + `source_locator` through a single assertion core; a deliberately lineage-dropping
stub chunker fed the same core **must fail**, proving the pack is non-vacuous (the same "honest
reverse proof" used by the `VectorStore` and `SourceConnector` packs). A new chunker inherits the
whole pack by adding one line to `CHUNKER_IMPLS`.

## What this is not

`contextual` retrieval shipped as an **index-text** concern, not a chunker — see
[`contextual.md`](contextual.md) (W4a). `parent-child` + layout-awareness shipped as
`LayoutAwareChunker` (W4b), sentence-window + semantic as `SentenceWindowChunker` /
`SemanticChunker` (W10) — all opt-in, the default byte-identical. RAPTOR's recursive-cluster
**multi-granularity tree** is a *retrieval-side* capability, not a chunker — it consumes chunks and
builds a summary tree above them; see [`raptor.md`](raptor.md) (W10). Richer family-extractor
structure and retrieval-time window/parent expansion remain named follow-ups.
