---
covers:
  - src/ragspine/retrieval/postprocess.py
verified-against: a9f5b31
---

# Post-retrieval postprocessor chain — the node-postprocessor stage (W8)

Live contract for the deterministic, opt-in post-retrieval chain that runs **after** the ⭐ rerank
exit and **before** prompt assembly. The `NodePostprocessor` Protocol
(`postprocess(query, results) -> results`, over the RESTRICTED-stripped snippet dicts) + three
processors + the `make_postprocessor` factory live in `retrieval/postprocess.py`; the seam is wired
through `link/narrative_link.py`'s `build_narrative_retriever(postprocessor=…)` /
`NarrativeIndexRetriever`. Benchmarks LlamaIndex `LongContextReorder` / `MMRPostprocessor` /
`SentenceEmbeddingOptimizer`, Haystack `LostInTheMiddleRanker` / `DiversityRanker`, LangChain
`ContextualCompressionRetriever`.

## The processors

| spec | class | what | model? | deps |
|---|---|---|---|---|
| `mmr` | `MMRPostprocessor` | Maximal Marginal Relevance diversity de-dup | no | none |
| `lost_in_middle` / `litm` / `long_context_reorder` | `LostInTheMiddlePostprocessor` | most-relevant to the two ends | no | none |
| `compress` / `compression` | `CompressionPostprocessor` | extractive sentence compression (reuses W5 `LexicalOverlapJudge`) | no (opt-in `compressor` seam) | none |
| `"a,b,…"` | `ChainPostprocessor` | ordered composition | — | — |

- **`MMRPostprocessor`** — greedily pick the candidate maximizing `λ·rel − (1−λ)·max_sim(selected)`.
  **Relevance = input rank** (first = highest), so it inherits whatever ordering upstream produced
  (rerank / RRF) rather than re-reading a raw score. **Similarity = lexical Jaccard** over the CJK-aware
  token sets (block vectors aren't retrieval-time available yet — embedding similarity is a *follow-up*).
  Ties break by input order (strict `>` + ascending scan → smallest index wins) → deterministic. Optional
  `top_n` truncates, so a near-duplicate pushed down the order is **actually dropped** (saves context
  window). `λ` default `0.5` (classic Carbonell-Goldstein).
- **`LostInTheMiddlePostprocessor`** — canonical LITM reorder: even input ranks → head, odd ranks →
  reversed tail, so the most-relevant items land at **both ends** and the least-relevant sit in the middle
  (Liu et al. 2023, *Lost in the Middle*). Pure reorder, zero model.
- **`CompressionPostprocessor`** — deterministic extractive default: split each snippet into sentences,
  keep those whose query content-token coverage clears `threshold` (**reuses the W5
  `groundedness.LexicalOverlapJudge`** — same lexical-overlap machinery), keep the single best sentence
  when none clear it (never emits empty). The heavy path is the opt-in **`compressor`** seam
  (`(query, text) -> text`) for LLMLingua-2 / LLM compression behind `[llm]` — carried as a follow-up.

## "Opt-in" without changing the default loop

`make_postprocessor(spec)` mirrors W2's `make_reranker`:

- `make_postprocessor(None | "none" | "")` → `None`. `build_narrative_retriever(postprocessor=None)` then
  attaches no chain, so `NarrativeIndexRetriever.retrieve` output is **byte-identical**.
  `ServiceConfig.postprocessor` defaults to `"none"`. MMR / lost-in-the-middle are deterministic and
  *could* be default-on, but ship **opt-in** to preserve byte-identity (recommended-on, not on-by-default).
- `make_postprocessor("mmr")` / `"lost_in_middle"` / `"compress"` → the single processor.
- A comma spec (`"mmr,lost_in_middle"`) → a `ChainPostprocessor` applying each in order.
- `RAGSPINE_POSTPROCESSOR` selects the spec from env; unknown specs raise `ValueError` listing the
  available names (corespine `Registry`, case/space/hyphen-insensitive).

## Provenance — never broken (the W4a index_text vs chunk.text layering)

Compression must not damage provenance. It writes the compressed text to a **separate `prompt_text` key**
(`postprocess.PROMPT_TEXT_KEY`) that `agent._snippet_text` prefers for prompt assembly, and leaves the
original **`text`** plus **every reference field** (`source_locator` / `doc_id` / `chunk_id` / `title` /
`scores` / `sensitivity`) byte-identical. This is the same layering as W4a contextual retrieval
(`index_text` affects only the index/embed text, `chunk.text`/citation untouched): here `prompt_text`
affects only the prompt text, the citation payload (`doc_id` + `source_locator`) is untouched, and each
kept sentence is a **verbatim substring** of the original. When no compression chain is attached, no
snippet carries `prompt_text` and `_snippet_text` falls back to `text` — byte-identical.

## RESTRICTED isolation — inherited, not re-implemented

The chain runs **inside `NarrativeIndexRetriever.retrieve`, after** the `link/` exit strips
`sensitivity == RESTRICTED` (the same two-exit rule as W2/W6b). A postprocessor only ever
reorders / de-dups / compresses that **already-stripped subset**; it never reads chunks directly and never
fabricates a snippet, so RESTRICTED can neither enter a processor nor surface downstream.

- **Frozen by** `tests/retrieval/postprocess/test_postprocess_isolation.py`: a real `NarrativeIndex` over a
  `ChunkStore` seeded with a normal + a RESTRICTED chunk, retrieved through the `mmr,lost_in_middle,compress`
  chain, never surfaces the RESTRICTED text (in `text` or `prompt_text`); the **reverse-proof** hands a
  RESTRICTED snippet *directly* to each processor and shows it passes through / gets compressed — proving the
  protection lives at the **upstream exit**, not in the postprocessor, so the conformance assertion has teeth.

## Determinism

Same input → same output: MMR ties resolve by input order (stable), LITM is a pure index permutation, and
extractive compression is a verbatim sentence filter — all zero-model, zero-network, byte-reproducible.
Frozen across `tests/retrieval/postprocess/` (MMR de-dup + tie-stability, LITM two-ends, compression
denoise + provenance + verbatim-extract, chain composition, factory `none`/env/comma, byte-identity).

## Follow-ups (carried out of W8)

- **Embedding-based MMR similarity** (vs lexical Jaccard) once block vectors are retrieval-time available.
- **LLMLingua-2 / LLM compression** adapter behind the `compressor` seam (`[llm]`), same
  "first-pull-then-offline" honesty as W1/W2.
- An **A/B** measuring compression token-savings vs answer-accuracy on the W5 groundedness ratchet.
