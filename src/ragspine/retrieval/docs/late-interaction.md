---
covers:
  - src/ragspine/retrieval/rerank/colbert.py
  - src/ragspine/retrieval/rerank/splade.py
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# Retrieval representation upgrade — ColBERT late-interaction + SPLADE learned-sparse (W11)

Live contract for the two optional retrieval-representation rerankers behind the rerank exit:
**ColBERT** (token-level multi-vector *late interaction*, `rerank/colbert.py`) and **SPLADE**
(neural *learned-sparse* term-expansion, `rerank/splade.py`). Both implement the **existing**
`ListwiseJudge` Protocol (`rerank/listwise_rerank.py`), plug into the **existing** `make_reranker`
factory (`rerank/cross_encoder.py`), and run *inside* the unchanged `listwise_rerank(...)`
orchestration — so W11 adds two new *judges*, opening no new exit and changing no default.

Benchmarks **Weaviate / Vespa / Jina ColBERT · Vespa SPLADE · LlamaIndex `ColbertIndex` /
`ColbertRerank` · the 2025 ColBERTv2 / SPLADE-v3 frontier**.

## Landing decision — reranker, not retrieval backend (the pragmatic increment)

The PRD offers each representation as *retriever or reranker*. W11 ships both as **rerankers**
(re-scoring the base retrieval's candidates), the **minimal-change, seam-reusing** landing:

- **ColBERT-as-reranker** (vs ColBERT-as-retriever): a reranker needs **no multi-vector index** —
  it embeds only the handful of already-retrieved candidates per query, scores them by MaxSim, and
  reorders. This is exactly `ColbertRerank` in LlamaIndex. A multi-vector `VectorStore` index
  (PLAID / MUVERA / Vespa-style, N vectors per doc, large at rest) is a **follow-up**.
- **SPLADE-as-reranker** (vs SPLADE-as-sparse-retriever): scoring candidates by learned-sparse dot
  product is the minimal landing. A sparse inverted index fused with BM25 via RRF (SPLADE's native
  retrieval mode) is a **follow-up**.

Landing both on the `ListwiseJudge` seam means they inherit W2's orchestration, isolation,
determinism, and `make_reranker` wiring verbatim — no new seam, no default-path change.

## The judges

| reranker spec | class | representation | score | deps / extra |
|---|---|---|---|---|
| `colbert` / `colbertv2` / `late_interaction` | `ColbertReranker` | token-level **multi-vector** | **MaxSim** late interaction | `[colbert]` (`fastembed`, Apache-2.0) |
| `splade` / `splade_pp` / `learned_sparse` | `SpladeReranker` | **learned-sparse** term-expansion | sparse **dot product** | `[splade]` (`fastembed`, Apache-2.0) |

Both are lightweight, **deterministic, offline**, CPU-only. Default models are permissive
(ADR 0009 ≤ Apache-2.0): ColBERT `colbert-ir/colbertv2.0`, SPLADE `prithivida/Splade_PP_en_v1`.
Construction is **lazy** (no `fastembed` import, no model load until first `judge`), so the core
still imports with zero extras and runs offline on the identity/RRF fallback (ADR 0009 / 0005).

### ColBERT MaxSim (`maxsim`, `colbert.py`)

`maxsim(query_vectors, doc_vectors) = Σ_{i∈Q} max_{j∈D} cos(q_i, d_j)` — each query **token** takes
its best cosine against **any** document token, summed. ColBERT token vectors are L2-normalized
(dot = cosine); `maxsim` still normalizes for zero-vector robustness (zero vector → 0, the repo
cosine convention). It is a **pure function**, unit-tested directly (precise MaxSim math) apart from
the fastembed plumbing. `query_embed` (query prefix) embeds the query, `embed` (doc prefix) the
candidates — the standard ColBERT query/doc asymmetry.

### SPLADE sparse dot (`sparse_dot`, `splade.py`)

`sparse_dot(q, d) = Σ_{t∈ q∩d} q[t]·d[t]` over the two `{term_id: weight}` sparse vectors — a
learned-sparse generalization of BM25 (interpretable term weights, but neural term-expansion instead
of raw tf·idf). A **pure function**, unit-tested directly (symmetric; iterates the smaller vector).
Each candidate's `SparseEmbedding(indices, values)` is normalized to `{int: float}`.

Both `judge(query, candidates)` return a **descending-score permutation** of candidate indices; ties
keep input (RRF) order (stable `sorted(reverse=True)`) → deterministic. Empty candidates → `[]`
(no model load). A returned-count mismatch raises (never a silently-wrong ranking).

## "Opt-in" without changing the default loop — reuses `make_reranker`

`make_reranker(spec)` (the W2 reranker factory) now dispatches all three local brains:

- `make_reranker(None | "none")` → `None`. `build_narrative_retriever(reranker=None)` keeps the
  **existing** judge selection (`ProviderListwiseJudge(provider)` if a provider is injected, else no
  second pass). **The default loop is byte-identical** — `ServiceConfig.reranker` defaults to
  `"none"`, so nothing reranks by ColBERT/SPLADE unless explicitly configured.
- `make_reranker("colbert")` / `make_reranker("splade")` → the respective reranker (lazy; first
  `judge` raises a friendly `pip install ragspine[colbert]` / `[splade]` if `fastembed` is absent).
  Passed as `reranker=`, it **takes precedence over** the provider LLM judge.
- `make_reranker("auto")` is **unchanged** — it still resolves to `CrossEncoderReranker` (the default
  local brain); ColBERT/SPLADE are explicit named opt-ins, never picked by `auto`.
- `RAGSPINE_RERANKER` selects the spec from env; `RAGSPINE_COLBERT_MODEL` / `RAGSPINE_SPLADE_MODEL`
  override the model for the respective specs (data-driven env map in `make_reranker`).

Registration lives in `cross_encoder.py` (the reranker-factory hub) so `make_reranker` discovers the
classes; the impl modules `colbert.py` / `splade.py` depend only on `corespine` (no SDK at import).

## RESTRICTED isolation — inherited, not re-implemented

Both are plugged in **as a `ListwiseJudge`**, so they run *inside* `listwise_rerank`, which already
excludes `sensitivity == RESTRICTED` candidates from the judge (never scored, kept in their original
RRF position; all-RESTRICTED → judge not called at all). Opening these seams therefore **cannot**
bypass the two-exit rule — RESTRICTED text never reaches `LateInteractionTextEmbedding.embed` /
`SparseTextEmbedding.embed`.

- **Frozen by** `tests/retrieval/rerank/test_colbert_isolation.py` and
  `tests/retrieval/rerank/test_splade_isolation.py`: a RESTRICTED candidate fed through
  `listwise_rerank(…, ColbertReranker()/SpladeReranker())` is never seen in any `embed` call and
  keeps its position; the **reverse-proof** test shows the same reranker *does* embed/score the text
  when handed it directly (bypassing the seam) — proving the assertion has teeth (a regression that
  leaked RESTRICTED past `listwise_rerank` would turn it red). Same idiom as W2's cross-encoder.

**Provenance** is untouched: rerankers only reorder the candidates the upstream `link/` exit already
produced (with real `doc_id` / `source_locator`); no snippet is fabricated, no lineage dropped.

## Determinism + offline honesty

- **Determinism (conformance):** pin model + `fastembed` version → CPU `onnxruntime` is
  byte-reproducible; two fresh reranker instances yield identical ranks for the same input (ties by
  stable descending sort). Frozen by the `@pytest.mark.network` tests in `test_colbert.py` /
  `test_splade.py` (first run downloads weights; CI runs `-m "not network"`, never touching the
  network). The fake-fastembed unit tests freeze the MaxSim / sparse-dot score→rank ordering + tie
  stability deterministically with **no network or install** — the `fake_colbert` / `fake_splade`
  fixtures map tokens to orthogonal one-hot vectors / term-frequency sparse vectors, so the late
  interaction / sparse-dot math is genuinely exercised and controllable.
- **Offline honesty:** identical to W1/W2 — `fastembed` downloads the ONNX weights from HuggingFace
  on first use, then caches (**first-pull-then-offline**), *not* first-run-offline. A truly
  first-run-offline default needs the weights shipped as a data-pack (the `ocrspine-models` pattern)
  — tracked as a **follow-up** in `docs/prd-quality-depth.md`.

## Follow-ups (carried out of W11)

- **Multi-vector `VectorStore` index for ColBERT-as-retriever** (PLAID / MUVERA / Vespa-style; N
  vectors per doc, large at rest) — the heavy retrieval-backend half; W11 ships the reranker half.
- **SPLADE sparse inverted index fused with BM25 via RRF** (SPLADE's native retrieval mode) — the
  heavy sparse-retrieval half; W11 ships the reranker/scoring half.
- **A/B measuring** ColBERT-rerank vs cross-encoder-rerank vs identity/RRF, and SPLADE vs BM25, on
  the W5 groundedness/eval harness (the eval-delta the depth gap matrix asks for); plus storage-cost
  honesty for the multi-vector indexes.
