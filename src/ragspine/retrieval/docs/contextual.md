---
covers:
  - src/ragspine/retrieval/contextual.py
verified-against: c3d6a7f2621d50ecf6d29a00180a6bf13ab92c16
---

# Contextual retrieval — deterministic context header (W4a)

Live contract behind the **Contextual retrieval** row of
[`docs/prd-quality-depth.md`](../../../../docs/prd-quality-depth.md) (W4a, ⭐). The PRD is the
originating spec; this is the live contract. It is the **deterministic, zero-fabrication variant**
of Anthropic's contextual-retrieval technique.

## The problem

`chunk.text` is a bare paragraph join. The doc-level context (title / entity / period / section
heading) lives only in the chunk's sidecar metadata fields and **never enters the indexed/embedded
text** — so BM25 and the embedding model can't situate a chunk by its document, entity, or period.

## What ships — the index-text layer

`retrieval/contextual.py` builds a **deterministic** context header from a chunk's *already-known*
controlled-vocab metadata and prepends it to the **index/embed text only**:

```python
build_context_header(chunk)   # "[文档:{title} · 实体:{entity} · 期间:{period} · 章节:{heading}]"
contextual_index_text(chunk)  # f"{header}\n{chunk.text}"  (or chunk.text when header is empty)
```

The header is assembled from `title · entity · period · heading` — **only non-empty fields, fixed
order** (deterministic), every value drawn from existing metadata. **No LLM, no fabrication.**
`getattr`-with-default reads each field, so a `StoredChunk` (which has no `heading` column) is safe.

## Why this doesn't break provenance / citation / byte-identity

The header lives in **one layer only — the text handed to the tokenizer / embedder** — never in
`chunk.text`:

- `chunk.text`, `source_locator`, and the **"chunk text = original substring"** contract are
  untouched, so citations and the captured retrieval golden (`test_byte_identity_golden`) are intact.
- It is **opt-in**. `HybridRetriever` and `NarrativeIndex` take `index_text_fn: IndexTextFn | None`,
  defaulting to `None`. With `None` the module-level `_index_text(chunk, None)` returns `chunk.text`
  verbatim — BM25 tokenization, lazy block-vector embedding, *and* at-ingest persisted embedding all
  byte-identical to before. Injecting `contextual_index_text` switches **all three** to the headered
  text, while the **query is always embedded plain** (context situates documents, not queries).
- **RESTRICTED isolation is unaffected** — the header is index-only; RESTRICTED chunks are still
  dropped at the two exits (`link/`, `rerank/`) and never persisted by the default
  `IsolationFirstPolicy`. Context is metadata, never a citable fact.

## Config selection (mirrors `make_chunker`)

`make_index_text_fn(spec=None) -> IndexTextFn | None`:

- `None` / `"none"` → `None` (caller falls back to `chunk.text` — byte-identical default).
- `"default"` / `"deterministic"` / `"on"` / `"contextual"` → `contextual_index_text`.
- anything else → `ValueError` listing the choices.
- with `spec=None`, the env var `RAGSPINE_CONTEXTUAL` supplies the spec.

## Service switch: `RAGSPINE_CONTEXTUAL_INDEX=off|heading|full` (default `off`)

`make_contextual_index_mode` normalizes the switch (`none` → `off`, the old enable aliases → `full`);
`make_index_text_fn` maps `off` → `None`, `heading` → `heading_index_text` (`[章节:<heading path>]` + text, the header
built by `build_context_header(chunk, fields)` with the heading field only), `full` → `contextual_index_text`.
Wiring: `ServiceConfig.contextual_index`, `RetrievalPreset.contextual_index` (facade), `build_narrative_retriever(
contextual_index=)`, the narrative worker payload, the nl-gold eval's `--contextual-index`.

- **Prompt text unchanged.** Snippet `text` is the chunk, `prompt_text` the page window — neither carries the header,
  so the heading is never duplicated for the LLM.
- **Whole-page units.** With a `fn` injected, the `page+child` whole-page BM25 unit gets `heading =
  page_parent.pages.page_heading(page chunks)`: every heading segment of the page once, de-duplicated — not one
  header per chunk, so BM25 term frequency is not inflated.
- **Persisted vectors.** `ChunkVectorIndex.sync(..., contextual_index=)` embeds the index text and computes each doc's
  signature over it (off → identical to the old signature), so a switch re-embeds exactly the docs whose index text
  changed. The db records `contextual_index` (absent = `off`; `migrating:<mode>` while a sync runs, so an interrupted
  sync is never used). `open_vector_channel` → `check_compatible(..., contextual_index=)` raises
  `VectorIndexMismatchError` on any mismatch — no silent mixing.
- **Default `off`.** On the 71-page AIA deck `heading` lifts GS recall@1 / MRR (BM25 dedup r@1 38% → 51%) and keeps
  the real-LLM nl-gold score (A 86.4%±0, B 86.4%±0 vs 84.9%±2.6), but route B page recall@1 drops 59% → 53% and the
  probe set's BM25 recall@5 slips 2–3 points, so it is not a strict no-regression and stays opt-in.

## The LLM adapter is a seam, not built here

A higher-recall **LLM-written** per-chunk context blurb (behind `[llm]`) is just another
`IndexTextFn` injected through the same `index_text_fn` seam — core unchanged. It is deliberately
**not** implemented this round (the deterministic header is the default; the LLM blurb is opt-in,
non-deterministic, and gated by the anti-fabrication discipline). Follow-up.

## Tests

`tests/retrieval/test_contextual.py` pins: header determinism + empty-field skipping + empty-header
fallback; `chunk.text` stays pure after `contextual_index_text`; the header **enters the BM25 index
when opt-in** (a query that hits only the header's entity code retrieves the chunk) and **does not
when default**; an end-to-end `NarrativeIndex` ingest→retrieve through `chunk_store` proving the
controlled-vocab header survives persistence; and the `make_index_text_fn` spec/env factory.
