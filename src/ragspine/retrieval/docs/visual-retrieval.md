---
covers:
  - src/ragspine/retrieval/vision/
verified-against: 11bf4362ea0e8a230f6a9749f16ba0ed5a1662d5
---

# Visual-document retrieval — ColPali page-as-image late interaction (W12)

Live contract for the optional **visual-document retriever** (`retrieval/vision/colpali.py`): embed a
document **page as an image** and do **late interaction directly on the image** (visual patch
multi-vectors vs query token multi-vectors, **MaxSim**) — **no OCR→text step**, preserving layout /
chart / figure visual structure. It is a strong route **parallel to** (not replacing) the family
OCR→text scanned path (`extraction/extractors/pdf_scanned_extractor.py`, W3a): visual retrieval wins on
chart / figure-dense financial reports; OCR→text wins on offline / deterministic / CPU.

Benchmarks **LlamaIndex ColPali · Weaviate / Vespa ColPali · the 2025 ColPali / ColQwen2 frontier**.

## Landing decision — seam + deterministic orchestration + gpu-marked real model (the pragmatic increment)

Same pragmatism as W11: ship the **usable, testable** increment now, defer the heaviest end-to-end to a
GPU box.

- **`VisualEmbedder` seam** (`@runtime_checkable` Protocol) — `embed_query(query) → query token
  multi-vectors` + `embed_images(images) → per-page patch multi-vectors`. Injectable, so the MaxSim
  scoring / `page→image` orchestration / RESTRICTED isolation are **deterministically unit-tested with a
  fake visual embedder** on any machine (no GPU, no model, no network).
- **`ColPaliVisualRetriever`** — the orchestration: score page-image candidates by MaxSim, rank
  descending (ties keep index order → deterministic), emit visual hits with provenance.
- **Visual MaxSim reuses W11** — `maxsim` is **re-exported from `rerank/colbert.py`** (the *same*
  function object; `vision.colpali.maxsim is rerank.colbert.maxsim`), not re-implemented. Late
  interaction over image patches is the same `Σ_{i∈Q} max_{j∈D} cos(q_i, d_j)` as over text tokens.
- **`page→image` reuses `pypdfium2`** — `render_pdf_pages(path)` renders each PDF page to PNG bytes
  (the `pdf_scanned_extractor` render idiom), unit-tested offline (pypdfium2 is a base dependency).
- **Real ColPali backend** — `ColPaliVisualEmbedder` via fastembed `LateInteractionMultimodalEmbedding`
  (`Qdrant/colpali-v1.3-fp16`), lazy, behind `[colpali]`. Real load/encode is `@pytest.mark.gpu`
  (`tests/retrieval/vision/test_colpali_gpu.py`; CI runs `-m "not gpu"`, self-skips without fastembed).

**Follow-ups (carried out of W12):** a CPU / quantized ColPali path if one matures; **fusing** visual
hits with the OCR→text channel (RRF over both routes); honest GPU / throughput benchmarking; the ColQwen2
vs ColPali model choice + fastembed availability; a persistent multi-vector visual index at scale.

## The pieces

| symbol | role |
|---|---|
| `VisualPage` | a page candidate: `doc_id` / `page_no` / `image` (PNG bytes) / `sensitivity` / `title` / `source_locator`; `.locator` defaults to `"{doc_id}#page{page_no}"` |
| `VisualEmbedder` (Protocol) | `embed_query` + `embed_images` — the injectable visual late-interaction seam |
| `render_pdf_pages(path)` | pypdfium2 `page → PNG bytes` (1-based); unreadable / zero-page → `[]` |
| `ColPaliVisualRetriever` | orchestration: RESTRICTED-at-the-door → embed → MaxSim → ranked visual hits |
| `ColPaliVisualEmbedder` | real fastembed backend (`[colpali]`, lazy, gpu-marked); default `Qdrant/colpali-v1.3-fp16` |
| `make_visual_embedder(spec)` | opt-in factory; `None`/`"none"` → `None`; `colpali`/`colqwen2`/`visual` → backend |

Construction is **lazy** (no fastembed import, no model load until first encode), so the core imports
with zero extras and never touches the visual path unless opted in.

## "Opt-in" — the default loop is byte-identical

W12 is **entirely additive**: a new `retrieval/vision/` subpackage + an opt-in factory. Nothing in the
default text-retrieval loop imports or wires it — `build_narrative_retriever` / `answer_question` are
**untouched**, so retrieval + `answer_question` stay **byte-identical** (4-gate + W5 groundedness ratchet
green, 0 fabrication, demo `ALL CHECKS PASSED`). `make_visual_embedder(None)` → `None`;
`RAGSPINE_VISUAL_EMBEDDER` selects a backend from env; `RAGSPINE_COLPALI_MODEL` overrides the model.

## RESTRICTED isolation — a new exit, screened at the door (+ reverse-proof)

Visual retrieval is a **new path that could reach a prompt** (like W7 `GraphStore` / W10 RAPTOR), so it
is screened **at the door**, not via the text two-exit (`link/` + `rerank/`) which never sees page
images: `ColPaliVisualRetriever.__init__` drops every `sensitivity == RESTRICTED` page **before** it can
enter the visual index. A RESTRICTED page therefore **never** enters `self.pages`, is **never** handed to
`embed_images`, and **never** surfaces in a hit. All-RESTRICTED → empty index → `retrieve` returns `[]`
and the embedder is not called at all. `sensitivity` matching is case-insensitive (same convention as the
two exits).

- **Frozen by** `tests/retrieval/vision/test_colpali_isolation.py`: a RESTRICTED page fed to the
  retriever is never seen in any `embed_images` call and never appears in the output; the **reverse-proof**
  shows the same embedder *does* encode the page image when handed it directly (bypassing the retriever's
  door) — proving the protection lives in the retriever orchestration, not the embedder (a regression that
  leaked a RESTRICTED page past the door would turn it red). Same idiom as W11's rerank isolation.

**Anti-fabrication / provenance.** A visual hit is a **retrieval lead, not a citable fact**: `is_visual =
True` and `text = ""` — no fabricated body text, so the visual model can never inject a hallucinated
number into a citation. Numbers stay in the **structured channel**; the visual hit only points at a page
(`doc_id` + page `source_locator` + `page_no`). Provenance is carried, never fabricated.

## Determinism + honesty

- **Determinism (conformance):** the fake-embedder unit tests freeze the MaxSim score→rank ordering + tie
  stability deterministically with **no network / GPU / install** (tokens → orthogonal one-hot vectors, so
  the late-interaction math is genuinely exercised and controllable). The `@pytest.mark.gpu` tests assert
  the real model is byte-reproducible for the same input.
- **Offline honesty:** identical to W1/W2/W11 — fastembed downloads the ONNX weights from HuggingFace on
  first use, then caches (**first-pull-then-offline**), *not* first-run-offline.
- **Dependency vs model license (honest):** the **code dependency** fastembed is **Apache-2.0** (passes
  the ADR 0009 ≤ Apache-2.0 dependency-license gate). The **model weights** carry their own license —
  `Qdrant/colpali-v1.3-fp16`'s PaliGemma base is under the **Gemma license** (use restrictions; *not*
  ≤ Apache-2.0-permissive). Weights are runtime-pulled, *not* a packaged dependency, so they do **not** go
  through the CI dependency-license gate — but this is flagged honestly. **ColQwen2** (Qwen2-VL base,
  Apache-2.0) is the more-permissive configurable alternative (`RAGSPINE_COLPALI_MODEL`); the final
  permissive model choice + its fastembed availability is a **follow-up**.
- **GPU honesty:** ColPali is the heaviest route (vision-language model + first-pull weights) — opt-in,
  default-off, **never on the lean / CPU default path**. The heavy persistent multi-vector visual index +
  a full real-weights end-to-end benchmark are **GPU-box follow-ups**.
