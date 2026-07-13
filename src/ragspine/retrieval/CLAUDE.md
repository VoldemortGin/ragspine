---
covers:
  - src/ragspine/retrieval/
verified-against: 5b7374d36a85d0a1b64f8431981db5dae1d1a0b9
---

# retrieval — agent contract

Auto-loaded when working under `src/ragspine/retrieval/`. Keep terse; deep dives go
in `src/ragspine/retrieval/docs/`.

## What lives here

Narrative RAG. `chunking/` (paragraph-granular chunker + versioned store; the
`Chunker` seam — `chunker.py`: a `@runtime_checkable` `Chunker` Protocol + a
`DefaultChunker` that **delegates byte-identically** to `chunk_document` (entry
point/signature preserved, all callers untouched) + `make_chunker` /
`RAGSPINE_CHUNKER` config selector with `ragspine.chunkers` entry-point discovery,
so semantic / contextual / parent-child strategies become swappable — `layout_chunker.py`'s
`LayoutAwareChunker` (W4b) is the first non-default: heading-boundary sections + `parent_id`/`heading`
for small-to-big, opt-in via `make_chunker("layout")`, default still byte-identical; plus two W10
strategies, `sentence_window_chunker.py`'s `SentenceWindowChunker` (index single sentences + a
`window_text` for synthesis-time expansion) and `semantic_chunker.py`'s `SemanticChunker`
(embedding-distance-boundary splits, reusing `chunk_document` per segment), both opt-in / default
byte-identical; plus `domain_presets.py`'s thin `LayoutAwareChunker` subclasses — three override **only** the
heading predicate (`laws`: clause-hierarchy `第N条/款/项`; `qa`: each question + its following answer
share one `parent_id`; `book`: chapter-hierarchy `第N章`/markdown/numbered) and one (`parent_child` /
`small_to_big`, 批次 2.2 ③) overrides **only** the `_child_extra` hook to attach `window_text`=parent
section text + `parent_locator`=the section's **real** para span (small-to-big: precise child hit →
deterministic expansion to parent context, provenance points at the true parent locator), opt-in via
`make_chunker("laws"/"qa"/"book"/"parent_child")`, default byte-identical),
`contextual.py` (W4a — a deterministic, zero-fabrication context header built from controlled-vocab
metadata, injected into **index/embed text only** via the opt-in `index_text_fn` seam on
`HybridRetriever`/`NarrativeIndex`; `chunk.text`/citation untouched, default `None` = byte-identical),
`lexical/` (Okapi BM25, CJK uni+bigram, RRF fusion — `HybridRetriever` delegates
its vector **scoring** to the `VectorStore` seam), `vector/` (injectable embedding
backends, default none = pure BM25; + the pluggable `VectorStore` seam — `store.py`
+ `make_vector_store` (a lazy-loader **registry** over the built-ins, with an
**entry-point auto-discovery** fallback on the `ragspine.vector_stores` group so a
third-party backend is selectable by name with no core PR) — with an invariant-binding conformance kit in
`tests/conformance/` carrying an **exact-vs-approximate capability flag**, three real adapters
behind `[vector]` — `adapters/sqlite_vec.py` (embedded, exact) + `adapters/pgvector.py`
(Postgres, pg8000/BSD, exact) + `adapters/qdrant.py` (HNSW, qdrant-client local mode, **approximate**),
all three now scaling via **native ANN/KNN** (vec0 `MATCH` / pgvector HNSW / Qdrant HNSW) that narrows a
candidate pool then an **exact `_cosine` re-rank** finalizes top-k (`store._pool_size` + `store._rerank`;
the pool covers the true top-k for the conformance datasets so `sqlite_vec`/`pgvector` stay exact)
— and `persistence_policy.py` gating what is written at rest), `rerank/` (the ⭐精排 exit:
`listwise_rerank.py` orchestration + `ListwiseJudge` Protocol with RRF-fallback + RESTRICTED isolation;
judges — LLM listwise via `link/`, and three offline local brains all selected by `make_reranker`
(`cross_encoder.py`): the **cross-encoder** `cross_encoder.py` (fastembed `TextCrossEncoder`,
`[rerank]`, W2), plus two W11 retrieval-representation rerankers — **ColBERT late-interaction**
`colbert.py` (fastembed `LateInteractionTextEmbedding`, token-level multi-vector **MaxSim**,
`[colbert]`) and **SPLADE learned-sparse** `splade.py` (fastembed `SparseTextEmbedding`, sparse
**dot product**, `[splade]`) — all deterministic, offline, opt-in via `make_reranker`
('colbert'/'splade'), default `none` byte-identical),
`link/` (adapter wiring retrieval into the agent),
`corrective.py` (**W6b corrective retrieval / CRAG, opt-in default-off**: `CorrectiveRetriever` wraps any base
`NarrativeRetriever` and generalizes the lone `retry_without_filters` fallback into a **bounded** (`max_retries`
clamped ≤2), **deterministic**, **traced** grade→act loop — retrieve→grade; low grade → `drop_filters` →
`rewrite_query`; still low → refuse `[]`. Default grader `LexicalOverlapGrader` (zero model/network); LLM/CE
grader = opt-in `RelevanceGrader` seam. `make_corrective_retriever` / `RAGSPINE_CORRECTIVE`, default `none`
returns base unchanged (byte-identical). **Isolation inherited** — only ever returns a subset of the base's
RESTRICTED-stripped output, never reads chunks directly),
`postprocess.py` (**W8 post-retrieval postprocessor chain, opt-in default-off**: a `NodePostprocessor` Protocol
+ three deterministic zero-model processors — `MMRPostprocessor` (diversity de-dup, `λ·rel − (1−λ)·max_sim`,
rank relevance + lexical-Jaccard similarity), `LostInTheMiddlePostprocessor` (most-relevant to both ends), and
`CompressionPostprocessor` (extractive sentence compression **reusing W5 `LexicalOverlapJudge`**; opt-in
`compressor` seam for LLMLingua-2 / LLM). `make_postprocessor` / `RAGSPINE_POSTPROCESSOR` (comma spec →
`ChainPostprocessor`), default `none` → no chain → `NarrativeIndexRetriever.retrieve` byte-identical. Runs
*after* the `link/` RESTRICTED strip, so **isolation is inherited** (subset/reorder/compress only). Compression
writes a separate `prompt_text` key (agent prefers it) — original `text` + all reference fields untouched
(**provenance never broken**, the W4a index_text layering)),
`raptor.py` (**W10 RAPTOR recursive-cluster multi-granularity tree, opt-in default-off** — the second
global-synthesis route parallel to W7b narrative GraphRAG). `build_raptor_tree` drops RESTRICTED at the
door, builds leaves from chunks, then recurses: **deterministic threshold clustering** (`cluster_by_similarity`
— cosine≥τ edges + union-find connected components, the W7b `detect_communities` idiom, zero randomness) +
a per-cluster **`is_synthesis=True` summary** (never citable as fact; numbers stay structured) carrying the
**union of its members' provenance** (`⊆` leaf lineage, never fabricated). `RaptorSummarizer` seam: a
deterministic zero-LLM `ExtractiveRaptorSummarizer` default + an opt-in `LLMRaptorSummarizer` (`[llm]`,
degrades to extractive). `RaptorTree.retrieve` is collapsed-tree multi-granularity (leaf **or** theme);
`RaptorRetriever` (opt-in `NarrativeRetriever` wrapper) appends `is_synthesis`-tagged summary snippets after
the base's citable leaves. `make_raptor_summarizer` / `make_raptor_retriever` + `RAGSPINE_RAPTOR*`, default
`none` returns base unchanged (byte-identical)),
`vision/` (**W12 ColPali visual-document retrieval, opt-in default-off** — a route **parallel to** the family
OCR→text scanned path (`extraction`, W3a), not replacing it: embed a document **page as an image** and do
**late interaction directly on the image** (visual patch multi-vectors vs query token multi-vectors, **MaxSim**),
**no OCR→text**, preserving layout / chart / figure structure. `colpali.py`: a `VisualEmbedder` Protocol +
`ColPaliVisualRetriever` orchestration — **visual MaxSim re-uses `rerank/colbert.maxsim`** (same function object),
**`page→image` re-uses `pypdfium2`** (`render_pdf_pages`) — + a real fastembed `LateInteractionMultimodalEmbedding`
backend (`ColPaliVisualEmbedder`, `[colpali]`, lazy, `@pytest.mark.gpu`) + `make_visual_embedder` factory. Default
`none` ⇒ `None`, nothing in the default loop wires it ⇒ byte-identical. **Isolation is a new exit screened at the
door**: RESTRICTED pages are dropped at index construction (never embedded, never surfaced); visual hits are
`is_visual` retrieval leads (`text=""`, provenance carried), never a citable-fact source — numbers stay
structured. **Model-license honesty**: fastembed code is Apache-2.0 (passes the dependency gate); the default
`Qdrant/colpali-v1.3-fp16` weights are Gemma-licensed (runtime-pulled, flagged) — ColQwen2 (Qwen2-VL/Apache-2.0)
is the more-permissive `RAGSPINE_COLPALI_MODEL` alternative.

批次 2.2（检索产品化配置，对标 Dify dataset retrieval 的产品化面）——三块新增，全部离线确定性、
默认路径字节不变：
`filtering/` (**① 元数据过滤**): `metadata_filter.py` 的 `FilterCondition`/`MetadataFilter` —
打分**之前**的确定性条件过滤（最小算子集 `eq/ne/in/nin/gt/gte/lt/lte/between`，字符串字典序、缺字段不命中），
**只收窄**候选（`apply` 恒返回保序子序列），故绝不绕过 RESTRICTED（link/rerank 双出口照常剔除）；经
`HybridRetriever.search(metadata_filter=)` / `NarrativeIndex.retrieve(metadata_filter=)` 接入，默认 `None`＝字节不变。
`automatic.py` 的 `FilterExtractor` 缝 + `make_filter_extractor`（默认 `none`→`None`，opt-in
`ControlledVocabFilterExtractor` 确定性受控词表规则抽取；LLM 抽取器 opt-in 适配器接入）——抽取产物**只能**是
`MetadataFilter`（结构上无途径进答案通道），经 `NarrativeIndex(filter_extractor=)` 注入。
`routing/` (**② 多库/多路检索路由**): `multi_index.py` 的 `LibraryIndex`/`MultiIndexRetriever`（实现 A 线
`NarrativeRetriever` 协议）——并行多库检索后**跨库 RRF 融合**（复用 `lexical.rrf_fuse`），每条结果带 `library_id`
（provenance 保留库来源维度）；隔离**继承**自各库 base 出口（RESTRICTED 绝不出域）。`router.py` 的
`LibraryRouter` 缝 + `make_library_router`（默认 `none`＝扇出全部库＝模式 a；`keyword` 确定性 `KeywordLibraryRouter`
按库描述词面匹配＝模式 b；LLM 路由 opt-in），零重叠时回落全部库不饿死召回。
`mode.py` (**④ economy 模式包装**): `RetrievalMode` + `make_retrieval_mode`（`ServiceConfig.retrieval_mode`）—
把既有纯 BM25 关键词检索（`embedding_backend=None`）包装成显式 `economy` 预设（**零 embedding 成本**，装配时绝不
构造 embedding 后端/向量库），与 `hybrid`/`vector` 在同一配置面切换；默认 `auto`＝混合（embedding 按 `ServiceConfig.embedding`
装配，字节不变）。

## Invariants

- **RESTRICTED isolation** — sensitivity-`RESTRICTED` content is stripped at two
  exits, `link/` and `rerank/`, before it can reach a prompt. Both must stay. The
  `VectorStore.where` pushdown is an *optional third* enforcement point, never a
  replacement — the retriever's `where` carries the 5 recall dims, never `sensitivity`.
- **At-rest persistence** — the default `PersistencePolicy` (`IsolationFirstPolicy`)
  **never persists a `RESTRICTED` chunk's vector** at ingest; only `PersistEverything`
  (opt-in, RESTRICTED-tier db) does. See `docs/invariants.md` + `docs/vector-store.md`.
- **New 批次 2.2 modes inherit isolation, never add a bypass** — metadata filtering only
  **narrows** candidates (a filter can never surface RESTRICTED — the two exits still strip it);
  multi-index routing operates on already-stripped per-library snippets (RESTRICTED never fuses in)
  and keeps the `library_id` provenance dimension; economy mode is the existing BM25 exit path with
  the vector channel off. Every new mode is bound in `tests/conformance/test_metadata_filter_invariants.py`
  / `test_multi_index_isolation.py` / `test_retrieval_mode_invariants.py` (parametrized + reverse-proof).

## Read before editing

- **Vector wiring is byte-identical on purpose.** `HybridRetriever` routes vector
  scoring through `VectorStore.query`, not an inline cosine loop. To keep results
  bit-stable: embed **candidates only** (prefilter strictly before any `embed_texts`),
  pass `k=len(candidates)`, build `where` with the exact `if val is not None` rule
  (so `""` is a real filter), and keep `best_vector` defaulting via `.get(cid, 0.0)`.
  A captured golden pins the triples — don't weaken it. Candidate `chunk_id`s are
  assumed unique (the `by_id` dict already does).
- **`NarrativeIndex` embeds-and-persists at ingest** (policy-gated), invalidates by
  `doc_id` (`delete(where={"doc_id": …})`, *not* blast-all), and retrieves with
  `HybridRetriever(manage_vectors=False)` — the retriever queries the store and never
  re-embeds chunks. Keep `_record_metadata`'s `doc_id` (it powers doc-scoped delete and
  is *not* in the retrieval `where`, so scoring stays byte-identical). The direct
  `HybridRetriever` path keeps `manage_vectors=True` (lazy embed) and stays byte-identical.

## Deep dives

- [`docs/vector-store.md`](docs/vector-store.md) — the `VectorStore` seam, its
  byte-identical wiring into `HybridRetriever`, the sqlite-vec / pgvector / qdrant adapters,
  the exact-vs-approximate capability flag, the isolation pushdown, and sensitivity-gated
  persistence (`PersistencePolicy` + embed-at-ingest).
- [`docs/chunker.md`](docs/chunker.md) — the `Chunker` seam: the `Protocol`, the
  `DefaultChunker` byte-identical delegation to `chunk_document`, the `make_chunker`
  factory + entry-point discovery, the provenance conformance pack, `LayoutAwareChunker`
  (W4b: heading-boundary layout + parent-child / small-to-big), and the two W10 strategies
  `SentenceWindowChunker` / `SemanticChunker` — all opt-in, default byte-identical.
- [`docs/raptor.md`](docs/raptor.md) — the W10 RAPTOR recursive-cluster multi-granularity tree:
  deterministic threshold clustering, the `is_synthesis` summary discipline (never a citable fact,
  never-fabricated provenance), the `RaptorSummarizer` seam (extractive default + LLM opt-in/degrade),
  collapsed-tree multi-granularity retrieval, the RESTRICTED isolation-at-the-door + reverse-proof,
  and the `make_raptor_*` / `RAGSPINE_RAPTOR*` opt-in factories (default byte-identical).
- [`docs/contextual.md`](docs/contextual.md) — contextual retrieval (W4a): the deterministic,
  zero-fabrication context header, the `index_text_fn` opt-in seam (index/embed text only, citation
  + byte-identity preserved), and the `make_index_text_fn` / `RAGSPINE_CONTEXTUAL` selector.
- [`docs/embedding-backend.md`](docs/embedding-backend.md) — the `EmbeddingBackend` seam: the
  real-semantic `OnnxEmbeddingBackend` default (W1, fastembed/`[embed-onnx]`), the `auto`
  default-on-dense mechanism that keeps the lean BM25 contract byte-identical, determinism +
  first-pull-then-offline honesty, and the re-baselined A/B semantic-gain numbers.
- [`docs/rerank.md`](docs/rerank.md) — the reranker seam: the offline `CrossEncoderReranker`
  (W2, fastembed `TextCrossEncoder`/`[rerank]`) as a swappable `ListwiseJudge`, the `make_reranker`
  factory + `none`/`auto` opt-in mechanism that keeps the default loop byte-identical, the RESTRICTED
  isolation inherited from `listwise_rerank` (+ its reverse-proof), and determinism honesty.
- [`docs/late-interaction.md`](docs/late-interaction.md) — the W11 retrieval-representation rerankers:
  **ColBERT** late-interaction (`colbert.py`, token-level multi-vector MaxSim, `[colbert]`) and
  **SPLADE** learned-sparse (`splade.py`, sparse dot product, `[splade]`), both on the same
  `ListwiseJudge` seam + `make_reranker` factory, the reranker-not-retriever landing decision
  (multi-vector / sparse index = follow-up), inherited RESTRICTED isolation (+ reverse-proofs),
  determinism + first-pull-then-offline honesty, and the default `none` byte-identity.
- [`docs/visual-retrieval.md`](docs/visual-retrieval.md) — the W12 ColPali visual-document retriever:
  page-as-image late interaction (visual patch MaxSim, **re-using `rerank/colbert.maxsim`**), the
  `VisualEmbedder` seam + `ColPaliVisualRetriever` orchestration, `page→image` via `pypdfium2`, the real
  fastembed `LateInteractionMultimodalEmbedding` backend (`[colpali]`, lazy, gpu-marked), the
  RESTRICTED-at-the-door isolation (+ reverse-proof), the `is_visual`/`text=""` anti-fabrication stance, the
  code-vs-model license honesty (fastembed Apache-2.0 vs Gemma-licensed weights / ColQwen2 alternative), and the
  opt-in / byte-identical default.
- [`docs/postprocess.md`](docs/postprocess.md) — the post-retrieval `NodePostprocessor` chain (W8): MMR
  de-dup + lost-in-the-middle reorder + extractive compression, the `make_postprocessor` /
  `RAGSPINE_POSTPROCESSOR` factory + comma-chain, the opt-in / byte-identical `postprocessor=` seam on
  `build_narrative_retriever`, the `prompt_text` provenance layering, and the inherited RESTRICTED isolation
  (+ its reverse-proof).
