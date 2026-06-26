---
covers:
  - src/ragspine/retrieval/contextual.py
verified-against: 0ee12fc
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
