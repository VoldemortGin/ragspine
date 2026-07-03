---
covers:
  - src/ragspine/retrieval/rerank/cross_encoder.py
verified-against: dcb8fba
---

# Reranker seam — the ⭐ rerank stage's offline brain (W2)

Live contract for the local cross-encoder reranker behind the rerank exit. The `ListwiseJudge`
Protocol (`judge(query, candidates) -> list[int]`, descending-relevance ranks) lives in
`rerank/listwise_rerank.py`; the offline cross-encoder implementation + the `make_reranker`
factory live in `rerank/cross_encoder.py`. The `listwise_rerank(query, results, judge, …)`
orchestration (RESTRICTED isolation + degrade-to-RRF) is unchanged — W2 only adds a new *judge*.

## The judges

| reranker spec | class | offline brain? | deps / extra | offline |
|---|---|---|---|---|
| `none` / `None` | — (returns `None`) | — (identity/RRF, or injected LLM judge) | none | yes |
| `auto` | `CrossEncoderReranker` *if* `fastembed` importable, else `None` | yes / falls back | `[rerank]` | first-pull-then-offline |
| `cross_encoder` / `ce` / `ms_marco` | `CrossEncoderReranker` | **yes** | `[rerank]` (`fastembed`, Apache-2.0) | first-pull-then-offline |
| (injected) `ProviderListwiseJudge` | LLM listwise (`link/narrative_link.py`) | yes (higher-cost) | `[llm]` / any provider | no (API) |

> **W11 extension.** `make_reranker` (`rerank/cross_encoder.py`) is the **reranker-factory hub** for
> all three offline local brains: the W2 `CrossEncoderReranker` plus two W11 retrieval-representation
> rerankers — `ColbertReranker` (`colbert` / `colbertv2` / `late_interaction`, token-level
> multi-vector MaxSim, `[colbert]`) and `SpladeReranker` (`splade` / `splade_pp` / `learned_sparse`,
> learned-sparse dot product, `[splade]`). All three implement `ListwiseJudge` and share this
> orchestration + isolation + `make_reranker` selection verbatim; `auto` still resolves to the
> cross-encoder (ColBERT/SPLADE are explicit named opt-ins). See
> [`late-interaction.md`](late-interaction.md) for the W11 contract.

`CrossEncoderReranker` is the W2 deliverable: a **lightweight, deterministic, offline** rerank
brain. Default model `Xenova/ms-marco-MiniLM-L-6-v2` (Apache-2.0, ~80 MB) run on CPU via
**fastembed**'s `TextCrossEncoder` (Apache-2.0; bundles ONNX weights + `onnxruntime`, the same
runtime W1's `OnnxEmbeddingBackend` uses). It scores each `(query, candidate)` pair and returns a
descending-relevance permutation of candidate indices; ties keep input (RRF) order (stable
`sorted(reverse=True)`) so the output is deterministic. Construction is **lazy** (no `fastembed`
import, no model load until first `judge`), so the core still imports with zero extras and runs
offline on the identity/RRF fallback (ADR 0009 / 0005).

## "Opt-in" without changing the default loop

`make_reranker(spec)` mirrors W1's `make_embedding_backend`:

- `make_reranker(None | "none")` → `None`. `build_narrative_retriever(reranker=None)` then keeps
  the **existing** judge selection — `ProviderListwiseJudge(provider)` when a provider is injected,
  else no second pass. **The default loop is byte-identical**: `ServiceConfig.reranker` defaults to
  `"none"`, so nothing reranks by cross-encoder unless explicitly configured.
- `make_reranker("cross_encoder")` → `CrossEncoderReranker` (lazy; first `judge` raises a friendly
  `pip install ragspine[rerank]` if `fastembed` is absent). Passed as `reranker=`, it **takes
  precedence over** the provider LLM judge — the local brain replaces the cloud one.
- `make_reranker("auto")` probes whether `fastembed` is importable: present → `CrossEncoderReranker`,
  absent → `None` (no rerank). `auto` is offered for parity with the embedding factory but is **not**
  the default (the default stays `"none"` so behavior is unchanged unless opted in).
- `RAGSPINE_RERANKER` selects the spec from env; `RAGSPINE_CROSS_ENCODER_MODEL` overrides the model
  for the `cross_encoder` specs.

## RESTRICTED isolation — inherited, not re-implemented

The cross-encoder is plugged in **as a `ListwiseJudge`**, so it runs *inside* `listwise_rerank`,
which already excludes `sensitivity == RESTRICTED` candidates from the judge (never scored, kept in
their original RRF position; all-RESTRICTED → judge not called at all). Opening this seam therefore
**cannot** bypass the two-exit rule — RESTRICTED text never reaches `TextCrossEncoder.rerank`.

- **Frozen by** `tests/retrieval/rerank/test_cross_encoder_isolation.py`: a RESTRICTED candidate fed
  through `listwise_rerank(…, CrossEncoderReranker())` is never seen in any `rerank` call's
  documents and keeps its position; the **reverse-proof** test shows the same reranker *does* score
  the text when handed it directly (bypassing the seam) — proving the conformance assertion has teeth
  (a regression that leaked RESTRICTED past `listwise_rerank` would turn the assertion red).

## Determinism + offline honesty

- **Determinism (conformance):** pin model + `fastembed` version → CPU `onnxruntime` is
  byte-reproducible; two fresh `CrossEncoderReranker` instances yield identical ranks for the same
  input (ties resolved by stable descending sort). Frozen by
  `tests/retrieval/rerank/test_cross_encoder.py::test_cross_encoder_real_deterministic_and_relevant`
  (`@pytest.mark.network` — first run downloads weights; CI runs `-m "not network"`, never touching
  the network). The fake-fastembed unit tests freeze the score→rank ordering and tie-stability
  deterministically with no network or install.
- **Offline honesty:** identical to W1 — `fastembed` downloads the ONNX weights from HuggingFace on
  first use, then caches (**first-pull-then-offline**), *not* first-run-offline. A truly
  first-run-offline default needs the weights shipped as a data-pack (the `ocrspine-models` pattern)
  — tracked as a **follow-up** in `docs/prd-quality-depth.md`, out of scope for W2.

## Follow-ups (carried out of W2)

- **A/B rerank effectiveness measurement.** W2 ships the reranker + orchestration wiring +
  determinism/isolation conformance; a ratcheted A/B that quantifies the precision lift of the local
  cross-encoder vs identity/RRF (the eval-delta the depth gap matrix asks for) is deferred to the W5
  groundedness/eval workstream.
