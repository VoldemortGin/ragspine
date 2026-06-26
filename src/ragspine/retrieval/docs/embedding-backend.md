---
covers:
  - src/ragspine/retrieval/vector/embedding_backends.py
verified-against: e634e99
---

# EmbeddingBackend seam — the default loop made semantic (W1)

Live contract for the embedding backends behind the vector channel. The `EmbeddingBackend`
Protocol (`embed_texts(list[str]) -> list[list[float]]`) lives in `lexical/retrieval.py`; the
implementations + the `make_embedding_backend` factory live in `vector/embedding_backends.py`.

## The backends

| spec | class | semantic? | deps / extra | offline |
|---|---|---|---|---|
| `none` / `None` | — (returns `None`) | — (pure BM25) | none | yes |
| `auto` | `OnnxEmbeddingBackend` *if* `[embed-onnx]` installed, else `None` | yes / falls back | `[embed-onnx]` | first-pull-then-offline |
| `onnx` / `fastembed` / `minilm` | `OnnxEmbeddingBackend` | **yes** | `[embed-onnx]` (`fastembed`, Apache-2.0) | first-pull-then-offline |
| `deterministic` | `DeterministicEmbeddingBackend` | **no** (lexical hash) | none | yes |
| `openai` | `OpenAIEmbeddingBackend` | yes | `[llm]` | no (API) |
| `qwen3` / `sentence-transformers` / `st` | `SentenceTransformerEmbeddingBackend` | yes | `[embed]` (torch) | first-pull-then-offline |

`OnnxEmbeddingBackend` is the W1 deliverable: a **lightweight, deterministic, real-semantic**
default. Default model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(Apache-2.0, 384-dim, **multilingual symmetric** sentence embedding — zh/en cross-lingual, no
query/passage prefix asymmetry, so it fits the single `embed_texts` Protocol) run on CPU via
**fastembed** (Apache-2.0; bundles ONNX weights + `onnxruntime`). Construction is **lazy**
(no `fastembed` import, no model load until first `embed_texts`), so the core still imports
with zero extras and runs offline on BM25 (ADR 0009 / 0005).

## "Default on dense" without breaking the lean contract

`HybridRetriever` already turns dense on whenever an `embedding_backend` is present
(BM25 + vector → RRF). W1 makes the **shipped default** resolve to one:

- `make_embedding_backend("auto")` probes whether `fastembed` is importable. Installed
  (`pip install ragspine[embed-onnx]`) → `OnnxEmbeddingBackend` (genuinely semantic hybrid,
  no config). Absent → `None` → pure BM25.
- `ServiceConfig.embedding` default is `"auto"`. **Lean runtime is byte-identical**: with no
  extra, `"auto"` → `None` → the exact BM25 path that shipped before (ADR 0005 preserved).
  Only the default *config string* changed `"none"` → `"auto"`.
- `None` / `"none"` / no-env still return `None` literally — the zero-dep BM25 contract is
  unchanged; `"auto"` is the only addition that can turn dense on, and only when the extra is
  present.

## Determinism + offline honesty

- **Determinism (conformance):** pin model + `fastembed` version → CPU `onnxruntime` is
  byte-reproducible; two fresh `OnnxEmbeddingBackend` instances yield identical vectors for the
  same input. Frozen by `tests/retrieval/vector/test_embedding_onnx.py::
  test_onnx_real_deterministic_and_crosslingual` (`@pytest.mark.network` — first run downloads
  weights; CI runs `-m "not network"`, never touching the network).
- **Offline honesty:** fastembed downloads the ONNX weights from HuggingFace on first use, then
  caches ("**first-pull-then-offline**"), *not* first-run-offline. A truly first-run-offline
  default needs the weights shipped as a data-pack (like `ocrspine-models`) — tracked as a
  **follow-up** in `docs/prd-quality-depth.md`, out of scope for W1.

## Re-baselined A/B (real semantic gain — was previously un-measurable)

`scripts/eval_retrieval_ab.py --embedding onnx --corpus data/golden/retrieval_ab_corpus.jsonl
--gold data/golden/retrieval_ab_real.jsonl` measures hybrid-ONNX vs BM25-only on the
cross-lingual / paraphrase golden set (12 zh↔en queries where word-overlap fails):

| metric | BM25-only | hybrid (ONNX) | hybrid (lexical-hash) |
|---|---|---|---|
| Recall@5 | 0.333 | **0.667** (+100%) | 0.500 |
| MRR (@5) | 0.292 | **0.542** (+86%) | 0.378 |
| Recall@3 | 0.333 | **0.667** | — |
| Recall@1 | 0.250 | **0.417** | — |

> The harness previously *could not* show semantic gain: each eval arm retrieved through a
> store-managed `NarrativeIndex` whose vectors were never persisted, so the vector channel was
> empty and hybrid always equalled BM25. `_eval_arm` now drives `HybridRetriever` directly
> (`manage_vectors=True`, lazy candidate embedding), so the vector channel actually scores —
> the disclaimer "proves harness correctness only, not semantic gain" is replaced by the
> measured numbers above. The lexical-hash backend's smaller lift is *not* semantic (it is
> BM25-correlated bucket overlap); only the ONNX column is a real cross-lingual gain.
