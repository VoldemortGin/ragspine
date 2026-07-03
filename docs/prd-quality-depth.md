# PRD — Quality Depth: out-engineer the ⭐ stages, tap the family stack, and measure groundedness

> **status:** proposed · **created:** 2026-06-26 · **methodology:** TDD — eval gate (red) → conformance (red) → implement to green
> Living backlog — like [`prd-breadth-via-adapters.md`](prd-breadth-via-adapters.md), it carries no `covers:` frontmatter; each shipped piece's contract doc lands under `src/ragspine/<domain>/docs/*.md`.
> **Companion to [`prd-breadth-via-adapters.md`](prd-breadth-via-adapters.md):** that PRD *rents the commodity surface* (🔧) through a uniform adapter contract; **this PRD spends the ⭐ budget** — it makes the *default* answer correct, turns the spine family's document stack into a compound moat, and adds the graph + groundedness measurement an anti-fabrication engine needs.
> Realizes [ADR 0001](adr/0001-dual-channel-determinism.md) (dual-channel determinism), [ADR 0006](adr/0006-quality-bar-invariants-and-benchmark.md) (quality bar), operating within [ADR 0005](adr/0005-lean-core-experimental-isolation.md) (lean core + extras) and [ADR 0009](adr/0009-dependency-and-framework-policy.md) (permissive-license-only, no framework lock-in).

## Problem statement

The breadth PRD got the **seams** right: `VectorStore`, `Extractor`, `Chunker`, `EmbeddingBackend`,
`ListwiseJudge`, `OcrBackend`, `SourceConnector` are all Protocols with offline defaults and a conformance
kit. Breadth is now a bounded, honest backlog. But breadth is *perception*; **depth is whether the answer is
right** — and an audit of the live engine surfaces five quality gaps the breadth PRD does not address, because
they are about the **quality of the owned ⭐ stages and the default loop**, not about adding optional adapters:

1. **The default loop is not actually semantic.** The default `EmbeddingBackend` is a `hashlib` lexical hash
   (explicitly non-semantic, `retrieval/vector/embedding_backends.py:170-182`) and dense is **off by default**
   (`embedding_backend=None` ⇒ pure BM25, `retrieval/lexical/retrieval.py:250-255,298`). So a plain
   `pip install ragspine` retrieves with **BM25 only**; the retrieval A/B harness itself caveats that its
   deterministic-hash backend "prove[s] harness correctness only, not semantic gain"
   (`cli/eval_retrieval_ab.py:13-17`). The hybrid machinery is real; the *default* never exercises it semantically.

2. **The ⭐ rerank stage has no offline brain.** Rerank is **LLM-only** (Claude listwise,
   `retrieval/rerank/listwise_rerank.py`), and its offline default is **identity pass-through** (RRF order).
   The single most cost-effective precision lever in modern RAG — a local cross-encoder — does not exist as a
   default, so the offline loop never reranks by meaning.

3. **The family's document stack is barely tapped.** ragspine wires **only `pdfspine`** (tables), and even
   there flattens styling. PPTX goes through `python-pptx`; OCR goes through **GPU-gated PaddleOCR-VL**;
   **`.docx` has no path at all** (`ingestion/structured/ingestion.py:438`). Worse, the scanned-PDF OCR seam
   is **fully built and tested but never called** — a `scanned` verdict only *enqueues a review item*
   (`ingestion.py:581-591`), so scanned PDFs are never actually OCR'd in the default pipeline. The family ships
   `ocrspine` (pure-Rust, offline, deterministic PP-OCRv5) and `pptspine`/`docspine` (information-preserving
   table models) — exactly the offline, invariant-clean producers the spine wants — and uses almost none of them.

4. **Anti-fabrication is asserted on the narrative side, never measured.** The structured channel is
   deterministically guarded (model prose discarded, rewrite-to-"not found"). The narrative channel
   **trusts model prose and only forces a citation** (`agent/agent.py:416-419`); nothing checks the answer is
   actually *entailed by* the retrieved snippets. The 4-gate eval scores numeric exact-match, citation-match,
   refusal, and clarification — there is **no faithfulness/groundedness gate and no free-text answer-accuracy
   gate** (`eval/qa_eval.py`). The brand's headline invariant has a measurement hole exactly where hallucination
   actually happens.

5. **No graph, no multi-hop, no GraphRAG.** Retrieval is strictly flat top-k; the agent is single-shot
   rule-routed retrieve-then-generate. There is **zero** knowledge-graph machinery (confirmed exhaustively),
   and GraphRAG is named once — as out-of-scope, "its own PRD" (`prd-breadth-via-adapters.md:291`, the PRD that
   doesn't exist). Queries the dual channel structurally cannot answer — *"compare X against its peers,"
   "roll up the subsidiaries," "trace this metric's derivation," "what changed across the portfolio and why"* —
   have no path.

**The need:** make the *default* answer correct (1, 2, 4), convert the family stack from "commodity to rent"
into an **owned compound moat** (3), and add the **graph + multi-hop** reasoning the dual channel lacks (5) —
**without** spending the determinism / anti-fabrication / provenance invariants that are the product.

## Strategy (the decision)

> Breadth rents the 🔧 surface. **Depth owns the ⭐ surface and proves the 🛡 invariants.**
> Three moves, in priority order:
> **(A) Fix the default** so out-of-box retrieval is genuinely semantic and reranked.
> **(B) Own the document stack** — the family already produces offline, deterministic, invariant-clean
> extractors/OCR; wire them and the pdf→ppt→doc→ocr pipeline becomes a moat no breadth framework can rent.
> **(C) Add graph + measure groundedness** — a charter-native structured relation graph for multi-hop, and a
> faithfulness eval that finally *measures* anti-fabrication on the narrative side.

Each ⭐ stage keeps the breadth PRD's five-part contract (Protocol · offline default · thin adapter · registry ·
conformance). The difference: this PRD specifies **the offline default's quality and the family-owned
implementation**, where the breadth PRD specified only that the seam exists. Every new model-bearing piece
follows ADR 0009: a **lazy adapter behind an extra**, permissive-license-only, core imports zero SDKs.

## Workstreams

Marks per stage: 🛡 touches an invariant · ⭐ quality-critical · 🔧 commodity. Phase tags **P0/P1/P2** as in the
breadth PRD.

### W1 — Make the default loop actually semantic ⭐  (P0)

**Gap:** default embedding = non-semantic hash; dense off by default ⇒ BM25-only out-of-box.

**Deliverable:** a real, lightweight, **offline, deterministic** semantic embedding default — a small ONNX
sentence-embedder (e.g. `bge-small` / `all-MiniLM-L6-v2`) run via a permissive runtime (`onnxruntime` /
`fastembed`, both Apache/MIT), behind a new `[embed-default]` (or folded into a CPU-only `[embed]`). Turn
**dense on by default** in `HybridRetriever` when a semantic backend is present, so the shipped loop is
genuinely hybrid (BM25 + semantic → RRF). Keep the lexical-hash backend as the **zero-dep fallback** (no extra
installed) so the lean default still runs (ADR 0005). Re-baseline the retrieval A/B (`cli/eval_retrieval_ab.py`)
against the real default so "semantic gain" becomes a *measured*, ratcheted number, not a disclaimed one.

- Determinism: ONNX inference is deterministic on CPU; pin the model + opset; conformance asserts
  byte-identical embeddings across two runs.
- Lean smoke stays green with **no** extra installed (lexical-hash path).

> **✅ SHIPPED.** `OnnxEmbeddingBackend` (`retrieval/vector/embedding_backends.py`) is a real,
> lightweight, deterministic semantic backend behind a new **`[embed-onnx]`** extra
> (**fastembed**, Apache-2.0 — bundles ONNX weights + `onnxruntime`, no torch). Default model
> `paraphrase-multilingual-MiniLM-L12-v2` (Apache-2.0, 384-dim, **multilingual symmetric** — zh/en
> cross-lingual, fits the single `embed_texts` Protocol). Registered as `onnx`/`fastembed`/`minilm`
> in `make_embedding_backend`, plus a new **`auto`** spec = "ONNX if the extra is importable, else
> `None` (pure BM25)". **Dense-on-by-default** lands by flipping `ServiceConfig.embedding` to
> `"auto"`: with `[embed-onnx]` installed the shipped loop is genuinely hybrid (BM25 + ONNX → RRF)
> with **no config**; with no extra it resolves to `None` and the lean BM25 path is **byte-identical**
> (ADR 0005 preserved — only the default config *string* changed). `None`/`"none"` still mean pure
> BM25 literally. **Re-baselined A/B** (`cli/eval_retrieval_ab.py`, real `--embedding onnx` on the
> cross-lingual/paraphrase golden set): **Recall@5 0.333 → 0.667 (+100%), MRR 0.292 → 0.542 (+86%)**
> — the "proves harness correctness only, not semantic gain" disclaimer is replaced by measured
> numbers (the harness `_eval_arm` was also fixed: it now drives `HybridRetriever` directly so the
> vector channel actually scores; before, the eval arm's store had no persisted vectors and hybrid
> always equalled BM25). Determinism + cross-lingual gain frozen by
> `tests/retrieval/vector/test_embedding_onnx.py` (the real-model assertions are `@pytest.mark.network`,
> skipped by `make ci`'s `-m "not network"` so CI never hits the network). Contract:
> `retrieval/docs/embedding-backend.md`.
> *Follow-up:* fastembed downloads weights from HF on first use then caches ("**first-pull-then-offline**"),
> not first-run-offline. A truly first-run-offline default needs the ONNX weights shipped as a
> data-pack (like `ocrspine-models`) — deferred (see "Out of scope / follow-ups").

### W2 — Local cross-encoder reranker ⭐  (P1, breadth PRD already lists the seam)

**Gap:** rerank is LLM-only; offline default is identity.

**Deliverable:** a local **cross-encoder** reranker (`bge-reranker-base` / `ms-marco-MiniLM` via ONNX) behind
`[rerank]`, registered as a selectable `ListwiseJudge`/`Reranker` impl, offline and deterministic — the real
offline brain for the ⭐ rerank stage. The LLM listwise judge stays as the higher-cost option. The cross-encoder
inherits the **isolation conformance pack** (a `RESTRICTED` candidate is never scored/emitted), so opening this
seam cannot bypass the two-exit rule.

> Note: the breadth matrix already lists `Rerank … cross-encoder · Cohere · BGE [rerank]` as `✅ proto / ✗
> adapters, P1`. W2 is the **quality spec + first adapter** for that row — owned because rerank decides
> correctness.

> **✅ SHIPPED.** `CrossEncoderReranker` (`retrieval/rerank/cross_encoder.py`) is a real, lightweight,
> deterministic, **offline** rerank brain behind a new **`[rerank]`** extra (**fastembed**, Apache-2.0 —
> reuses its `TextCrossEncoder` + ONNX `onnxruntime`, the same runtime W1 uses, no torch). Default model
> `Xenova/ms-marco-MiniLM-L-6-v2` (Apache-2.0, ~80 MB, CPU). It implements the **existing**
> `ListwiseJudge` Protocol — scores each `(query, candidate)` pair and returns a descending-relevance
> permutation of candidate indices, ties resolved by stable `sorted(reverse=True)` (→ same input, same
> ranks). Registered as `cross_encoder`/`ce`/`ms_marco` in a new **`make_reranker`** factory (corespine
> `Registry`, mirroring `make_embedding_backend`), plus an **`auto`** spec ("cross-encoder if `fastembed`
> importable, else `None`"). **Default behavior unchanged**: `build_narrative_retriever` gained a
> `reranker=` seam and `ServiceConfig.reranker` defaults to `"none"` → `make_reranker` returns `None` →
> the existing judge selection (`ProviderListwiseJudge(provider)` if a provider is injected, else no
> second pass) is byte-identical; the cross-encoder is **opt-in** (config string / `RAGSPINE_RERANKER`)
> and, when selected, takes precedence over the LLM judge. **Isolation inherited, not re-implemented**:
> the cross-encoder runs *inside* `listwise_rerank`, which already excludes RESTRICTED candidates from
> any judge — so RESTRICTED text never reaches the cross-encoder (frozen by
> `tests/retrieval/rerank/test_cross_encoder_isolation.py`, with a **reverse-proof** that the assertion
> has teeth). Determinism + cross-encoder relevance frozen by `tests/retrieval/rerank/test_cross_encoder.py`
> (real-model assertions `@pytest.mark.network`, skipped by `make ci`'s `-m "not network"`; the
> fake-fastembed unit tests pin score→rank ordering + tie-stability with no network/install). Contract:
> `retrieval/docs/rerank.md`.
> *Follow-ups:* (1) same "first-pull-then-offline" weight-download honesty as W1 — a truly
> first-run-offline rerank default needs the ONNX weights shipped as a data-pack (deferred, see
> "Follow-ups"). (2) An A/B that **measures** the precision lift of the local cross-encoder vs
> identity/RRF (the eval-delta the gap matrix asks for) is deferred to the W5 eval workstream — W2 ships
> the reranker + wiring + determinism/isolation conformance.

### W3 — Tap the family document stack ⭐ (compound moat)  (P0 OCR-wiring · P1 formats)

The reframe: pdf/ppt/doc/ocr extraction is marked 🔧 in the breadth matrix because for a generic library it *is*
commodity-to-rent. **For this family it is owned** — the producers exist, are pure-Rust, offline, deterministic,
and OCR-internally consistent. Wiring them turns a rented surface into a moat.

- **W3a — `ocrspine` as the default `OcrBackend` + wire the scanned path (🛡⭐, P0).** Replace GPU-gated
  PaddleOCR-VL as the *default* OCR with `ocrspine` (pure-Rust PP-OCRv5 via `tract`, offline/deterministic, the
  same engine pdf/ppt/doc embed). **Critically, wire it into ingestion**: a `scanned`/`ocr_scan`/`mixed`
  verdict must invoke `pdf_scanned_extractor` with the `ocrspine` backend (today it only enqueues review —
  `ingestion.py:581-591`). This makes scanned PDFs actually retrievable, offline, with the same provenance and
  low-confidence→review discipline already built (`pdf_scanned_extractor.py:204-217`). PaddleOCR-VL stays as the
  optional high-accuracy adapter behind `[ocr]`. **Highest-leverage, lowest-risk item: pure plumbing, zero
  charter tension, immediately verifiable.**
  > **✅ SHIPPED.** Realized via pdfspine's `Page.find_image_tables` OCR API — pdfspine embeds ocrspine
  > PP-OCRv5, and since ocrspine has **no Python binding**, the family OCR is reached *through pdfspine*
  > (already a `[pdf]` dep). Default `OcrBackend = PdfSpineOcrBackend` (`pdf_spine_ocr@1`); `_ingest_pdf` now
  > calls `pdf_scanned_extractor.extract_grids` on `scanned`/`ocr_scan`/`mixed` verdicts (was enqueue-only);
  > low-confidence cells still route to review; PaddleOCR-VL stays the optional `[ocr]` adapter. Wiring tested
  > with a deterministic fake backend (no-GPU CI); the real `find_image_tables` signature/return were verified
  > against pdfspine 0.0.6. *Follow-up:* add `find_image_tables` to pdfspine's `.pyi` stub (a `# type: ignore`
  > bridges the stub gap for now).
- **W3b — `docspine` `.docx` Extractor (⭐, P1).** A new `Extractor` for Word: closes the missing format
  (the breadth matrix lists `DOCX … P1`), built on `docspine`'s first-class table model (gridSpan/vMerge/nested),
  emitting `StyledGrid` + narrative segments. Inherits the provenance + extractor conformance packs.
  > **✅ SHIPPED.** `docspine` (PyPI 0.1.0, pure-Rust DOCX, Apache-2.0 → passes the ADR 0009 ≤Apache-2.0
  > licence gate) added as the `[doc]` extra, lazy-imported. New `DocspineGridExtractor`
  > (`extraction/extractors/docspine_extractor.py`, `version="docspine@1"`): each top-level table → a
  > `StyledGrid` (`sheet="table{M}"`, `cell_ref="R{r}C{c}"` on the true grid column via a gridSpan-advancing
  > cursor), with merge spans best-effort preserved into the existing IR (`is_merged_origin` + `merge_span`
  > from `grid_span` / `vMerge` restart-continue) — *no IR change* (rich fills/nested-into-IR stays W3d;
  > nested tables emit a grid warning, never silently dropped). Wired into **both** channels: structured
  > dispatch (`_EXTRACTOR_BY_SUFFIX[".docx"/".docm"]` → facts, stamped `docspine@1`, locator
  > `sheet=table{M}!R{r}C{c}`) and narrative (`extract_docx_narrative` → body paragraphs as segments,
  > `para={N}`, tables skipped). Registered in the `mime → Extractor` registry (`DOCX_MIME` + `.docx`). Legacy
  > binary `.doc` (OLE/CFB) is deliberately *not* registered → stays "unsupported format". Tested with a pure
  > `zipfile`-synthesized minimal `.docx` (no binary fixture, no `python-docx`); wiring tested offline with a
  > fake extractor, plus real-docspine parse/ingest tests (offline, pure-Rust).
- **W3c — `pptspine` Extractor (⭐, P1).** Replace `python-pptx` with `pptspine` for the structured + narrative
  PPTX path (richer merges, autoshapes, notes, embedded-image OCR via the same `ocrspine`).
  > **✅ SHIPPED (additive, opt-in — *not* a default replacement).** `pptspine` (PyPI 0.1.0, pure-Rust
  > PowerPoint/OOXML, Apache-2.0 → passes the ADR 0009 ≤Apache-2.0 licence gate) added as the **`[ppt]`**
  > extra, lazy-imported. New `PptspineGridExtractor` (`extraction/extractors/pptspine_extractor.py`,
  > `version="pptspine@1"`): each native table → a `StyledGrid` (`sheet="slide{N}_table{M}"`,
  > `cell_ref="R{r}C{c}"`, `resolved_rgb=None`) with merge spans best-effort into the **existing** IR
  > (`is_merged_origin` + `merge_span` from pptspine's resolved `col_span`/`row_span`; swallowed
  > `hMerge`/`vMerge` continuation cells dropped) — **no IR change** (rich fills/nested-into-IR stays W3d).
  > **Why opt-in, not the default (the honest call): `python-pptx` is *not* lossy here — the default
  > `pptx_styled_extractor` already resolves theme/scheme fill colours, native charts, styled runs, and
  > speaker notes, which `pptspine` 0.1.0 does *not*** (and pptspine 0.1.0 returns only the *first* table
  > per slide — a graphicFrame-parse limit). So a naïve swap would **lose** colour/chart/note — a regression.
  > W3c therefore ships `pptspine` as the **richer-merges opt-in** alternative while the default `.pptx` path
  > stays `python-pptx` (byte-identical; every existing pptx_styled / colour / chart / note test green). Two
  > opt-in seams: (1) **registry selector** `"pptx+pptspine"` (`registry.PPTX_PPTSPINE_SELECTOR`) dispatches
  > to pptspine while `.pptx`/`PPTX_MIME` still resolve to `pptx_styled`; (2) **structured-dispatch injection**
  > `ingest_file(..., pptx_extractor=PptspineGridExtractor())` overrides the default for the `.pptx` branch
  > (mirrors the PDF `grid_extractor` seam), stamping `extractor_version="pptspine@1"` into fact lineage. Tested
  > with a pure `zipfile`-synthesized minimal `.pptx` (no binary fixture, no `python-pptx`); wiring tested
  > offline with a fake extractor, plus a real-pptspine parse/ingest e2e (`pytest.importorskip("pptspine")`,
  > offline pure-Rust). *Follow-up:* lifting colour/chart/note + multi-table-per-slide onto pptspine (so it can
  > become the default) tracks pptspine ≥ next release; richer fills/nested **into the IR** is W3d.
- **W3d — preserve table richness into the IR (⭐, P1).** Extend `StyledGrid`/`StyledCell` so merges/nested/fills
  from pptspine/docspine survive (today pdfspine tables set `resolved_rgb=None` and ppt/doc richness is unreached),
  so cell-level citations and color/structure semantics resolve to page→table→cell across all three formats.
  > **✅ SHIPPED — no IR schema change (the existing `StyledGrid`/`StyledCell` fields were enough).** The
  > family extractors already carry the richness; W3d just *fills the existing IR fields* instead of
  > hardcoding `None`/warning. Two gaps closed (merges already landed best-effort in W3b/W3c):
  > **(1) Cell fills → `resolved_rgb`.** docspine's `cell['fill']` (`<w:shd w:fill>`) and pptspine's
  > `cell['fill']` (`a:tcPr` solidFill/srgbClr) are both already resolved `'RRGGBB'` upper-hex (or
  > `None` for `auto`/no-fill / unresolved theme-scheme colours). A tiny shared `_normalize_fill`
  > (None/`auto`/`none`→None, else upper) maps them straight into the **existing** `StyledCell.resolved_rgb`
  > — so docx/pptx cell colour now flows the **existing SME-gated color-semantics path** (`extraction/color/`:
  > `cells_by_rgb`/`cluster_colors`/`detect_legend`/`apply_mapping`) and the ingestion review on-ramp
  > (`_grid_has_colored_cells` → "colour mapping unconfirmed → enqueue") **with zero ingestion change**
  > (the color path is format-agnostic; it keys off `rgb_tag_key()`). pdfspine stays `resolved_rgb=None`
  > by design (PDF has no colour semantics). **(2) Nested tables → independent `StyledGrid`s.** Only docspine
  > has nested tables (DrawingML/pptx tables can't nest). `docspine_extractor` no longer warns-and-drops:
  > each nested table (`cell['blocks']` `kind=='table'`) is emitted as its **own `StyledGrid`**, sheet-named
  > to encode the parent→child locator chain (`table{M}.cell{r}_{c}.nested{k}`, recursive for deeper nesting,
  > parent-grid breadcrumb warning preserved) — so a nested cell is fully citable and never silently lost.
  > `extract_grids` returns the flat `[parent, *nested…]` list in reading order; downstream consumers are
  > unchanged (dotted sheet names are just strings; an unattributable nested grid gracefully skips, never
  > fabricates). TDD: `make_docx`/`make_pptx` conftest fixtures gained `fill`/`nested` synthesis; red tests
  > (`resolved_rgb` non-None for filled cells; nested grid emitted + colour reaching `cluster_colors`) →
  > green, plus a real-docspine ingest test proving a colored `.docx` fires the SME-gated colour-review
  > enqueue. Contract: extractor docstrings + `extraction/CLAUDE.md`.

### W4 — Contextual retrieval + family-layout chunking ⭐  (P1, Chunker seam exists)

**Gap:** chunks are bare paragraphs (`chunk.text` is a raw paragraph join); doc context lives only in sidecar
metadata, never indexed. Chunking is fixed-char paragraph-greedy.

- **W4a — Contextual Retrieval (deterministic default + LLM adapter).** Before indexing, prepend a deterministic
  context header to each chunk — `title · entity · period · section-heading` (all already known, controlled-vocab,
  zero fabrication) — so the embedded/lexical text carries situating context (Anthropic's contextual-retrieval
  technique, deterministic variant). An **opt-in** LLM-written context blurb behind `[llm]` is the higher-recall
  adapter, gated by the anti-fabrication discipline (context is metadata, never a citable fact).

  > **✅ SHIPPED (deterministic core; LLM adapter = seam-only follow-up).** `retrieval/contextual.py` builds a
  > **deterministic** context header from a chunk's already-known controlled-vocab metadata
  > (`title · entity · period · heading`, **zero LLM, zero fabrication**) and exposes it as an `IndexTextFn`
  > (`contextual_index_text`). The header enters the **index/embed text only** — never `chunk.text` — so
  > `source_locator`, the citable original, and the **"chunk text = original substring"** provenance contract are
  > untouched and `test_byte_identity_golden` stays green. Wired as an **opt-in seam**: `HybridRetriever` /
  > `NarrativeIndex` default `index_text_fn=None` (BM25 tokenization **and** block-vector embedding both use
  > `chunk.text` — byte-identical); injecting `contextual_index_text` makes both channels (and at-ingest persisted
  > vectors) carry context while the query stays plain. Selected by `make_index_text_fn(spec)` /
  > `RAGSPINE_CONTEXTUAL` env (mirrors `make_chunker`). RESTRICTED isolation is unaffected — context is index-only,
  > RESTRICTED chunks are still dropped at the two exits and never persisted by the default policy. The LLM
  > context-blurb adapter is left as a seam (any other `IndexTextFn`, behind `[llm]`) — **follow-up**. Contract:
  > `retrieval/docs/contextual.md`.
- **W4b — Layout-aware + parent-child chunking (the family-unique lever).** A `Chunker` strategy that chunks on
  **structural boundaries from the family extractors** (headings, sections, table edges from pdfspine/docspine),
  plus parent-child / small-to-big retrieval (retrieve small, expand to parent for synthesis). The breadth matrix
  lists `semantic · contextual · parent-child` as `Chunker` P1 strategies; W4 specs them to **exploit family
  layout**, which generic loaders (which see only `to_text()`) cannot.

  > **◐ SHIPPED (layout-aware + parent-child seam; richer-family-structure = follow-up).** `LayoutAwareChunker`
  > (`retrieval/chunking/layout_chunker.py`) is a new `Chunker` strategy, opt-in via `make_chunker("layout" |
  > "parent_child")` / `RAGSPINE_CHUNKER` — **default stays `DefaultChunker`, byte-identical** (the Chunker seam's
  > equality + golden tests untouched). It splits on **structural boundaries** (a deterministic heading heuristic:
  > markdown `#`, numbered / `第N章` headings, short punctuation-free lines), **never merging across a section**;
  > each child carries `parent_id` (`{doc_id}#s{k}`, the small-to-big parent handle) + `heading`, with
  > `group_children_by_parent` for sibling expansion. Within a section it **reuses `chunk_document`** (same
  > budget-greedy / overlap / oversized-split), only remapping to **global** paragraph numbers so locators stay
  > citation-honest and `chunk.text` stays an original substring. It inherits the Chunker provenance conformance
  > pack (registered in `tests/conformance` `CHUNKER_IMPLS`). **Follow-up:** feeding the *richer* structure the
  > family extractors expose (heading levels, table edges from pdfspine/docspine), and persisting
  > `parent_id`/`heading` through `chunk_store` + retrieval-time small-to-big expansion. Contract:
  > `retrieval/docs/chunker.md`.

### W5 — Faithfulness / groundedness eval 🛡  (P1 → a new eval gate)

**Gap:** narrative answers are never checked for entailment against retrieved snippets; the anti-fabrication
invariant is unmeasured on the side where hallucination occurs.

**Deliverable:** a claim-level **groundedness** eval — RAGAS-shaped **faithfulness** (every answer claim entailed
by retrieved context), **context-precision/recall**, **answer-relevance** — with an **offline deterministic NLI
default** (a small ONNX entailment model behind `[eval]`) and an optional LLM-judge adapter. Add a free-text
**answer-accuracy** gate for narrative cases (today only numeric exact-match is scored). Wire it as a **new
ratcheted gate** in `qa_eval.py` alongside the existing four, and run the retrieval A/B with the W1 real
embedding so the harness measures real semantic gain. This is 🛡: it makes anti-fabrication a *measured*
regression lock on the narrative channel, not just an asserted one.

> **✅ SHIPPED (faithfulness + free-text answer-accuracy gates; ONNX-NLI / LLM-judge / context-precision-recall =
> follow-ups).** `src/ragspine/eval/groundedness.py` adds two **new ratcheted gates** wired into `qa_eval.py`
> beside the four命门: **faithfulness** (every claim in the *narrative* answer must be entailed by the retrieved
> context) and **answer-accuracy** (the free-text narrative answer must cover the expected doc's substantive
> content — narrative cases had no content-correctness score before, only citation/refusal/clarification).
> `GATE_METRICS` (the four命门) keep their **exact semantics**; `GROUNDEDNESS_METRICS = (faithfulness,
> answer_accuracy)` are **new keys in the same `report.metrics`**, so they fold into the **same baseline ratchet**
> automatically (`data/golden/qa_baseline.json` re-baselined with both at 1.0, gated in both `--mode tool` and
> `--mode agent`). The **default method is the offline deterministic lexical-overlap entailment** (`LexicalOverlapJudge`:
> claim entailed iff its content-token coverage by the context ≥ threshold) — **no model, no network**, so `make ci`
> gates it offline. **Honest limitation:** it is a *lexical proxy, not a real NLI* — blind to paraphrase / negation /
> numeric reversal; it catches the most common fabrication shape (a claim that introduces tokens — new
> entity/number/assertion — absent from the context). **Teeth (non-trivial):** unit tests show a synthetic answer
> that adds an un-entailed claim **fails** the gate while the faithful echo **passes**, including a proof on the
> **real eval KB + retriever** (fabrication appended to genuinely-retrieved context is caught). Context is observed
> **eval-side only** (`CaseOutcome.narrative_answer`/`retrieved_context`, agent mode re-runs the retriever) — the
> default `answer_question` loop is **byte-unchanged**. **Follow-ups:** the real ONNX-NLI judge (`[eval]`) and the
> LLM-judge (`[llm]`) behind the `EntailmentJudge` seam (`make_entailment_judge`); context-precision / context-recall
> / answer-relevance; composite-case narrative-segment faithfulness; and the W1/W2 real-embedding retrieval A/B
> ratchet. Contract: `src/ragspine/eval/CLAUDE.md`.

### W6 — Agentic depth: multi-hop & corrective retrieval ⭐ (opt-in, determinism-preserving)  (P2)

**Gap:** single-shot, rule-routed; decomposition is a deterministic Cartesian over explicitly-enumerated axes;
no HyDE / planning / self-RAG / corrective retrieval / multi-turn.

All of W6 ships **opt-in**, default-off, so the deterministic default loop (and its byte-identical eval) is
unchanged — the determinism invariant is preserved by construction.

- **W6a — LLM query decomposition** behind the existing `IntentParser`/`QueryRewriter` seams (ADR 0010 already
  decouples this): real multi-sub-question fan-out for "which region grew fastest and why" class queries.
  > **✅ SHIPPED (opt-in, default-off).** `agent/decompose.py`: a `QueryDecomposer` Protocol + `LLMQueryDecomposer`
  > (single-shot provider call → a JSON sub-question array; **bounded** by `max_subquestions`; **deterministic
  > degrade** to `[question]` on `ProviderError` / non-JSON / empty) + a `make_decomposer` / `RAGSPINE_QUERY_DECOMPOSE`
  > selector (`'none'`→None default; `'llm'`→only when a provider is injected). `answer_question` gains an opt-in
  > `decomposer=` param: **default `None` ⇒ the entire existing body runs verbatim (byte-identical)**; when injected
  > *and* the question truly splits (>1 sub-question), each sub-question re-runs the **full** `answer_question`
  > (`decomposer=None`, no recursion) and the sub-answers are **deterministically concatenated** (route
  > `decomposed`, sources de-duped, tool_results merged) — zero LLM in the synthesis itself. **Anti-fabrication +
  > security inherited per sub-question**: each re-runs the deterministic security gate (a competitor sub-question
  > is still out-of-scope-refused — home numbers never leak) and the found/not-found rewrite — decomposition only
  > changes *what is asked*, never bypasses a guard. The default `RuleIntentParser` deterministic Cartesian
  > (`expand_subtasks`) is untouched. Wired into the service via `ServiceConfig.query_decompose` (default `"none"`).
  > Frozen by `tests/agent/test_query_decompose.py` (byte-identity, fan-out + aggregation, competitor-subquestion
  > refusal, LLM-parse/bound/degrade). *Follow-up:* an LLM synthesis pass *over* the sub-answers (today's synthesis
  > is deterministic concatenation, each sub-answer already guarded); HyDE / planning.
- **W6b — Corrective retrieval (CRAG) / self-RAG**: relevance-grade retrieved context; on low grade,
  re-retrieve (drop filters / rewrite) or refuse — turning the single `retry_without_filters` fallback into a
  principled grade→act loop, with every action traced.
  > **✅ SHIPPED (opt-in, default-off).** `retrieval/corrective.py`: `CorrectiveRetriever` wraps any base
  > `NarrativeRetriever` and upgrades the lone `retry_without_filters` fallback into a **bounded, deterministic,
  > traced grade→act loop** — retrieve→grade; on low grade act in order (`drop_filters` → `rewrite_query`, **capped
  > at ≤2**: `max_retries` clamped to `0..2`); if still below `min_grade`, **refuse** (return `[]` → the narrative
  > channel honestly says "未检索到"; refusing weak context is the anti-fabrication-safe choice). The default grader
  > is the deterministic `LexicalOverlapGrader` (zero model / zero network — query content-token coverage by the
  > retrieved union); an LLM / cross-encoder grader is an **opt-in `RelevanceGrader` seam** (follow-up). Every step
  > emits a `GradeAction` trace via `emit_trace` (non-sensitive: action names + grades only, never snippet text).
  > `make_corrective_retriever` / `RAGSPINE_CORRECTIVE` selector — default `"none"` returns the base **unchanged**
  > (byte-identical). **Isolation inherited, not re-implemented**: the wrapper only ever returns a *subset* of the
  > base's already-RESTRICTED-stripped output and never reads chunks directly (frozen by
  > `tests/retrieval/corrective/test_corrective_isolation.py` — a real `NarrativeIndex` over a RESTRICTED chunk
  > yields zero RESTRICTED, with a reverse-proof that the chunk IS in the store). Wired via `ServiceConfig.corrective`.
  > *Follow-up:* the cross-encoder / LLM grader behind the `RelevanceGrader` seam.
- **W6c — Conversational memory**: a stateless→multi-turn upgrade (follow-ups, coreference) behind the service
  layer, with the security gate + isolation re-asserted per turn.
  > **◐ SHIPPED (minimal usable skeleton; LLM coreference + endpoint wiring = follow-up).** `service/conversation.py`:
  > `ConversationMemory` (a **bounded** deque of `ConversationTurn` storing **only** the prior turn's home
  > entity-code + period — non-sensitive metadata, never answer / value / chunk) + `resolve_followup` (deterministic
  > carry-forward: a structured/composite follow-up missing entity/period is augmented with the prior turn's home
  > slots via reverse-alias + period rendering, so the rule parser re-resolves them) + `ConversationSession.ask`.
  > **Security re-asserted every turn**: each `ask` re-runs the **full** `answer_question` (the deterministic gate
  > screens the augmented raw question); a competitor follow-up is still out-of-scope-refused, home context is
  > **never** carried into an out-of-scope question (`resolve_followup` returns the question unchanged the moment
  > `external_entity` is detected), and a refused turn is **never** remembered — so memory cannot leak RESTRICTED /
  > competitor or turn a refusal into an answer (frozen by `tests/service/test_conversation.py`, incl. a
  > "competitor-follow-up-after-home-turn still refused, 1702 never leaks" case). *Follow-up:* true LLM
  > coreference / pronoun resolution; a multi-turn FastAPI endpoint (the skeleton is programmatic today, not yet
  > endpoint-wired); the deterministic slot carry-forward is intentionally conservative, not full coreference.

### W7 — GraphRAG ⭐ (the headline) — two layers, charter-aligned  (P2)

GraphRAG is named once in the codebase, as deferred. Build it in two layers so the charter-native value lands
first and the fabrication-risky layer stays opt-in.

- **W7a — Structured relation graph (charter-native, deterministic, ships first).** A typed graph built
  **deterministically over the existing controlled dimensions** — no LLM triple extraction, no fabrication:
  - **entity ↔ subsidiary / parent** (roll-up) — from the company profile hierarchy,
  - **metric ↔ derived-metric** (derivation chains) — `common/company_profile.py` already encodes
    `Dimension.derived_from` / `derivation` single-hop maps; promote them to traversable edges,
  - **entity ↔ competitor / external-entity** — from the existing external-entity vocabulary,
  - **doc ↔ entity / metric** co-occurrence — from facts + chunk metadata (provenance-carrying).

  Substrate already exists: the `Fact` star-schema (`storage/fact_store.py`), the derivation map, and the
  doc→chunk hierarchy. This unlocks **multi-hop structured queries the flat top-k + exact SQL cannot do** —
  *peer comparison, subsidiary roll-up, derivation tracing, portfolio-wide deltas* — while staying deterministic
  and fully cited. This is the GraphRAG a finance fact-engine should have.

- **W7b — Narrative GraphRAG (opt-in, behind the anti-fabrication discipline).** Microsoft-style entity/relation
  extraction from narrative text + community detection + community summaries, for global/thematic ("what are the
  cross-cutting risks?") queries that local top-k misses. **Opt-in and provenance-disciplined**: every extracted
  edge carries `source_doc_id` + locator; community summaries are clearly-labeled syntheses, never citable facts;
  the structured channel still owns numbers (anti-fabrication unbroken). LLM extraction is non-deterministic →
  it lives behind `[graph]` + `[llm]`, never on the default path.

- **W7c — `GraphStore` seam (🔧, the breadth contract for W7).** A `GraphStore` Protocol (`upsert_nodes/edges`,
  `neighbors`, `subgraph`, `traverse`) with an **offline in-process default** (zero-dep adjacency, deterministic)
  + thin adapters (`networkx` in-proc / `kuzu` embedded / `neo4j`), permissive-license-only, inheriting a new
  **provenance + isolation conformance pack** (every node/edge carries lineage; a `RESTRICTED`-sourced node never
  surfaces in a traversal result). This is how W7 opens to graph backends without the spine rotting — the same
  mechanism the breadth PRD uses for `VectorStore`.

> **W7 SHIPPED (W7a ✅ · W7c ✅ · W7b ◐ skeleton).** A new `graph/` domain (`src/ragspine/graph/`), all
> opt-in/default-off — the default `answer_question`/retrieval/eval path is **byte-identical** (4-gate + W5
> groundedness ratchet green both modes, **0 fabrication**; demo `ALL CHECKS PASSED`).
> - **W7c `GraphStore` seam (✅).** `store.py`: a `@runtime_checkable GraphStore` Protocol (`upsert_nodes/edges`,
>   `get_node`, `neighbors`, `subgraph`, `traverse`, `count_*`) + the zero-dep deterministic default
>   `InProcessGraphStore` + `make_graph_store` / `RAGSPINE_GRAPH_STORE` registry (built-ins `in_process` +
>   `networkx`; third-party via the `ragspine.graph_stores` entry-point group). One real adapter shipped —
>   `adapters/networkx_store.py` (BSD-3, `[graph]`, lazy-imported, conformance-bound); `kuzu`/`neo4j` are reserved
>   seams (the entry-point group is live today; first-party adapters = follow-up). The **provenance + isolation
>   conformance pack** (`tests/conformance/test_graph_store.py` + the registry in `conftest.py`) binds every impl:
>   provenance round-trip, RESTRICTED-never-surfaces, determinism — each with an **honest reverse-proof stub that
>   must FAIL** (`_LeakyGraphStore`, `_LineageDroppingGraphStore`). Five-段式 = the `make_vector_store` paradigm.
> - **W7a structured relation graph (✅).** `relation.py` `build_relation_graph(profile, *, facts, chunks)` builds a
>   typed graph **deterministically over the controlled dimensions** — `parent_of` (home→subsidiaries),
>   `derives` (any dim's `derived_from`/`derivation`, so entity→geography *and* metric→derived-metric),
>   `competes_with` (home→external entities), `mentions` (doc→entity/metric co-occurrence from facts + chunks).
>   Zero LLM, zero fabrication, every node/edge lineage-carrying. `query.py` `GraphQuery` is the **opt-in multi-hop
>   entry** (standalone — never touches `answer_question`): `subsidiary_rollup` (walk `parent_of` → N exact
>   `FactStore.query` → summed total with each contributing `Fact` cited), `peer_comparison` (per-entity cited
>   facts), `derivation_trace` (walk `derives` edges with per-edge provenance) — the multi-hop a flat top-k +
>   exact SQL can't do, **fully cited**. Inherits the `SecurityGate` (competitor entity → refused, zero data) and
>   the store's RESTRICTED isolation (doc nodes carry chunk `sensitivity`; RESTRICTED docs never surface).
> - **W7b narrative GraphRAG (◐ skeleton + seam + follow-up).** `narrative.py`: `GraphExtractor` /
>   `LLMGraphExtractor` (provider→JSON, **every relation stamped with `source_doc_id`+`source_locator`**, bounded,
>   deterministic degrade — same idiom as `agent/decompose.py`), **deterministic** `detect_communities`
>   (connected-components), `CommunitySummarizer` / `LLMCommunitySummarizer` (summaries are **`is_synthesis=True`,
>   never citable facts**; numbers stay in the structured channel), `make_narrative_graph` /
>   `RAGSPINE_NARRATIVE_GRAPH` (default `None` = off, behind `[graph]`+`[llm]`). Fake-LLM tested. *Follow-up:*
>   Leiden/Louvain hierarchical communities, incremental extraction, claim-anchoring, global map-reduce query
>   orchestration — deliberately not built to avoid leaking non-determinism into the default path.

### Second batch — 对标主流竞品的缺口补全 (W8–W12)

After a round of benchmarking ragspine against the mainstream RAG stacks — **LlamaIndex · LangChain+LangGraph ·
Haystack · RAGFlow · Dify · Weaviate · Vespa · Jina · Cohere + the 2025 frontier** — W1–W7 above already
**shipped parity on the highest-leverage stages**: true semantic dense+BM25+RRF hybrid (W1), local cross-encoder
rerank (W2), family layout / strong-table / OCR extraction (W3), Contextual Retrieval + layout/parent-child
chunking (W4), faithfulness/groundedness eval (W5), agentic decomposition / CRAG / self-RAG / multi-turn (W6),
and a deterministic structured relation graph + `GraphStore` seam + narrative-GraphRAG skeleton (W7). The
**moat** that parity rides on — **anti-fabrication + provenance + the family's offline OCR / strong-table
extraction + the offline deterministic charter** — is exactly what none of those competitors have.

W8–W12 close the **remaining benchmarked gaps**, all under the same rules as the SHIPPED batch: **opt-in,
default loop byte-identical**, holding [ADR 0001](adr/0001-dual-channel-determinism.md) determinism +
[ADR 0005](adr/0005-lean-core-experimental-isolation.md) lean-core + [ADR 0009](adr/0009-dependency-and-framework-policy.md)
permissive-license / lazy-extra, **reusing the existing seams** (the W2 `Reranker` / a thin postprocessor chain,
`QueryRewriter`/`IntentParser`, `Chunker`, `EmbeddingBackend`/`VectorStore`), with every non-deterministic piece
(LLM, vision) **default-off**. They are a tracked backlog (status ✗ in the gap matrix), not yet shipped —
specified here to the same Workstream体例 as W1–W7. The same five-part contract applies (Protocol · offline
default · thin adapter behind an extra · registry · conformance); these workstreams *reuse* existing seams rather
than inventing new extension styles.

### W8 — Post-retrieval postprocessor chain ⭐  (P1)

**Gap:** after the W2 cross-encoder rerank, the reranked top-k goes straight to prompt assembly — there is **no
node-postprocessor stage**. Three things every mainstream stack ships are missing: (1) **diversity de-dup** —
near-duplicate chunks waste the context window; (2) **lost-in-the-middle reordering** — LLMs attend worst to the
*middle* of a long context (Liu et al. 2023, *Lost in the Middle*), yet the reranked order puts the best hits
exactly there; (3) **context / prompt compression** — verbose snippets dilute signal and burn tokens.

**Deliverable:** a deterministic **node-postprocessor chain** that runs *after* the W2 cross-encoder rerank and
*before* prompt assembly, on a thin `NodePostprocessor` seam (a `make_postprocessor` / `RAGSPINE_POSTPROCESSOR`
selector mirroring `make_reranker`, composing an ordered list of processors). Three processors — two
pure-deterministic, one with an opt-in heavy path:

- **MMR diversity de-dup** (Maximal Marginal Relevance, Carbonell & Goldstein 1998) — **deterministic,
  zero-model**: greedily pick the chunk maximizing `λ·relevance − (1−λ)·max-sim-to-already-picked`, dropping
  near-duplicates. Benchmarks LlamaIndex `MMRPostprocessor` / `SimilarityPostprocessor`, Haystack
  `DiversityRanker`.
- **Lost-in-the-middle reorder** — **deterministic, zero-model**: reorder the surviving set so the
  most-relevant land at the **head and tail** of the context and the least-relevant sit in the middle.
  Benchmarks LlamaIndex `LongContextReorder`, Haystack `LostInTheMiddleRanker`.
- **Context / prompt compression** — **deterministic extractive default**: a sentence-level relevance filter
  keeping only sentences whose query content-token overlap clears a threshold (reuses the W5
  `LexicalOverlapJudge` machinery, zero-model). The heavier path is **opt-in**: an abstractive / learned
  compressor behind `[llm]` (LLM extract) or `[onnx]` — **LLMLingua-2** token-level compression. Benchmarks
  LlamaIndex `SentenceEmbeddingOptimizer`, LangChain `ContextualCompressionRetriever`
  (`LLMChainExtractor` / `LLMLinguaCompressor`).

MMR + lost-in-the-middle are pure functions of (scores, lexical/embedding similarity) — byte-identical and
legitimately *could* be on by default; but to **preserve byte-identity of the shipped loop** they ship
**opt-in** (`make_postprocessor` default `None` ⇒ no chain ⇒ byte-identical), recommended-on rather than
on-by-default. The compression's abstractive / LLMLingua path is opt-in behind the extra; its extractive default
is deterministic. **Isolation inherited, not re-implemented**: a postprocessor only ever returns a
*subset / reorder* of the already-RESTRICTED-stripped rerank output (the W6b `CorrectiveRetriever` idiom), so it
cannot surface RESTRICTED. Benchmarks the full **LlamaIndex node-postprocessor chain · Haystack rankers ·
LangChain `ContextualCompressionRetriever`**.

*Follow-up:* embedding-based MMR similarity (vs lexical) once block vectors are retrieval-time available; the
LLMLingua-2 ONNX weight pull (same "first-pull-then-offline" honesty as W1/W2); an A/B measuring compression
token-savings vs answer-accuracy on the W5 gate (a depth item isn't done until the eval ratchet shows it
improved / held the answer).

> **✅ SHIPPED (deterministic core; LLMLingua-2 / LLM compression = seam-only follow-up).** `retrieval/postprocess.py`:
> a `NodePostprocessor` Protocol (`postprocess(query, results) -> results`, over the RESTRICTED-stripped snippet
> dicts) + three implementations — **`MMRPostprocessor`** (Maximal Marginal Relevance greedy `λ·rel −
> (1−λ)·max_sim`, relevance = input rank, similarity = lexical Jaccard, ties stable by input order; optional
> `top_n` truly drops the near-dup tail), **`LostInTheMiddlePostprocessor`** (canonical LITM: even ranks →
> head, odd ranks → reversed tail, so the most relevant land at both ends), **`CompressionPostprocessor`**
> (deterministic extractive default **reusing W5 `LexicalOverlapJudge`** — keep sentences whose query
> content-token coverage clears a threshold, keep the best sentence when none do; opt-in `compressor` seam for
> LLMLingua-2 / LLM). All three **deterministic, zero-model, offline**. Composed by **`make_postprocessor`** /
> `RAGSPINE_POSTPROCESSOR` (corespine `Registry`, mirroring `make_reranker`); a comma spec like
> `"mmr,lost_in_middle"` builds a `ChainPostprocessor`. **Default byte-identical**: `build_narrative_retriever`
> gained a `postprocessor=` seam and `ServiceConfig.postprocessor` defaults to `"none"` → `make_postprocessor`
> returns `None` → no chain → `NarrativeIndexRetriever.retrieve` output is unchanged (MMR / lost-in-the-middle
> are deterministic and *could* be on by default, but ship **opt-in** to preserve byte-identity). **Provenance
> never broken** (the W4a index_text-vs-chunk.text layering): compression writes to a separate **`prompt_text`**
> key that `agent._snippet_text` prefers for prompt assembly, leaving the original `text` + every reference field
> (`source_locator` / `doc_id` / `chunk_id` / `title` / `scores` / `sensitivity`) byte-identical, and each kept
> sentence is a verbatim substring of the original. **Isolation inherited, not re-implemented**: the chain runs
> *after* the `link/` exit strips RESTRICTED, only ever reorders/de-dups/compresses that already-stripped subset
> — frozen by `tests/retrieval/postprocess/test_postprocess_isolation.py` (real-index integration + a
> **reverse-proof** that a RESTRICTED snippet fed *directly* passes through, proving the protection lives at the
> upstream exit). Determinism / MMR de-dup / LITM two-ends / compression-denoise-and-provenance / chain / factory
> / byte-identity all frozen under `tests/retrieval/postprocess/`. Contract: `retrieval/docs/postprocess.md`.

### W9 — Query transformation ⭐  (P2, opt-in)

**Gap:** query-side transforms are deterministic-only today — `RuleIntentParser`'s controlled-vocab synonym
multi-query (`expand_subtasks` Cartesian) and W6a's opt-in LLM decomposition. There is no **HyDE** (hypothetical
document embeddings), no **RAG-Fusion** (LLM multi-query → per-query retrieve → RRF), no **step-back prompting**
(abstract the question to a more general one for better recall), and no **Adaptive-RAG** (route by query
complexity: no-retrieval / single-hop / multi-hop). Competitors ship all four.

**Deliverable:** a family of **opt-in, LLM-backed query transforms** on the existing `QueryRewriter` /
`IntentParser` seam ([ADR 0010] already decouples query rewriting), selected by a `make_query_transform` /
`RAGSPINE_QUERY_TRANSFORM` registry (mirroring `make_decomposer`), all **default-off and byte-identical** when
unselected:

- **HyDE** — an LLM writes a *hypothetical* answer to the query; that hypothetical doc is embedded and retrieval
  runs against *its* vector (better dense recall for under-specified queries). Behind `[llm]` + `[embed-onnx]`.
  Benchmarks LlamaIndex `HyDEQueryTransform`, LangChain `HypotheticalDocumentEmbedder`.
- **RAG-Fusion** — an LLM generates N query variants, each is retrieved independently, results fused by **RRF**
  (we already own RRF from W1). Benchmarks LlamaIndex `QueryFusionRetriever`, LangChain `MultiQueryRetriever`.
- **Step-back prompting** — an LLM derives a more abstract "step-back" question; both original + step-back are
  retrieved and merged (Zheng et al. 2023). A **deterministic variant** is possible (generalize up the
  controlled-vocab dimension hierarchy, zero-LLM) — carried as follow-up.
- **Adaptive-RAG** — an LLM (or deterministic heuristic) classifies query complexity and routes: no-retrieval
  (parametric) / single-hop / multi-hop (→ hands off to W6a decomposition). Benchmarks LangGraph's
  adaptive-rag.

The deterministic synonym multi-query (existing) and W6a decomposition (existing) are the **deterministic
basis**; W9 adds the LLM transforms as opt-in adapters over it. Every variant is LLM and **default-off** — the
byte-identical default loop is unchanged by construction. **Anti-fabrication + security inherited**: HyDE's
hypothetical doc is a *retrieval probe*, never a citable fact; each fused / step-back / routed sub-query re-runs
the deterministic security gate (a competitor variant is still out-of-scope-refused — the W6a idiom, home
numbers never leak). Benchmarks **LlamaIndex `HyDEQueryTransform` / `QueryFusionRetriever` · LangChain
`MultiQueryRetriever` / HyDE · LangGraph adaptive-rag**.

*Follow-up:* HyDE needs dense-on (W1's `auto`); the **deterministic step-back** (controlled-vocab generalization)
as a zero-LLM variant; an A/B measuring recall lift per transform on the W5 harness.

> **✅ SHIPPED (all four opt-in / default-off; default loop byte-identical — `answer_question` is untouched).**
> `agent/query_transform.py` adds the four LLM query transforms on the existing `QueryRewriter` / `IntentParser`
> seam (ADR 0010), all **default-off and byte-identical** when unselected:
> - **HyDE (✅).** `HyDERetriever` (a `NarrativeRetriever` wrapper, seam identical to the W6b
>   `CorrectiveRetriever`): a single provider call writes a **hypothetical answer document**, and retrieval runs
>   with **that document as the query text** (aligns the dense vector — and the BM25 tokens — with answer-shaped
>   passages). The hypothetical doc is a **retrieval probe only**: `base.retrieve` returns the real chunks with
>   real lineage, so the hypothetical doc **never enters a snippet / answer / citation** (frozen by
>   `tests/agent/test_query_transform.py`). Benchmarks LlamaIndex `HyDEQueryTransform` / LangChain
>   `HypotheticalDocumentEmbedder`.
> - **RAG-Fusion (✅).** `RAGFusionRetriever`: a provider call generates N query variants (bounded by
>   `max_variants`); the original + each variant is retrieved and the results are fused by **RRF — reusing the
>   `retrieval.rrf_fuse` we already own from W1** (keyed by `chunk_id`). Different layer from the *deterministic*
>   `GlossaryQueryRewriter` synonym multi-query: that is controlled-vocab synonyms, this is LLM-generated
>   variants. Benchmarks LlamaIndex `QueryFusionRetriever` / LangChain `MultiQueryRetriever`.
> - **Step-back (✅).** `StepBackRetriever`: a provider call derives a more abstract *step-back* question; the
>   original (specific) + step-back (broader background) results are merged by RRF (Zheng et al. 2023). The
>   **deterministic controlled-vocab step-back** stays a follow-up.
> - **Adaptive-RAG (✅).** `HeuristicComplexityClassifier` (the **deterministic default** — routes by
>   listed-axis count / comparison cues, zero LLM) + opt-in `LLMComplexityClassifier` (same "deterministic
>   default + opt-in model" idiom as W5 `EntailmentJudge`, deterministic degrade to the heuristic). `AdaptiveDecomposer`
>   **reuses the existing `answer_question(decomposer=)` seam** (implements `QueryDecomposer`): `multi` →
>   delegates to the W6a LLM decomposer fan-out; `simple`/`single` → returns `[question]` ⇒ the **normal
>   single-shot route, byte-identical** (so the deterministic default routing is never changed — adaptive is an
>   opt-in enhancement, not a replacement). Benchmarks LangGraph adaptive-rag.
>
> **Factories:** `make_query_transform(base, spec, *, provider)` / `RAGSPINE_QUERY_TRANSFORM` (default `none` →
> **returns `base` unchanged**, byte-identical; `hyde`/`rag_fusion`/`step_back` opt-in, and a **degrade to `base`
> when no provider is injected** — honest, never a no-op LLM) and `make_adaptive_decomposer(spec, *, provider)` /
> `RAGSPINE_ADAPTIVE` (default `none` → `None`). Wired into the service via `ServiceConfig.query_transform`
> (`open_narrative_retriever` wraps the base retriever, upstream of the W6b corrective wrap) and
> `ServiceConfig.adaptive` (`routes.py` decomposer selection) — **both default `none` ⇒ the agent/retriever path
> is byte-identical.** **Anti-fabrication + security inherited, not re-implemented:** every generated RAG-Fusion
> variant / step-back question passes the deterministic `SecurityGate` **before retrieval** — a competitor
> variant is dropped and never retrieved (the W6a idiom, home numbers never leak; frozen with a **reverse-proof**
> that a spy base never receives the competitor query); the three wrappers only ever reorder / fuse a *subset* of
> the base's already-RESTRICTED-stripped output (isolation inherited — frozen by a real-index integration test +
> reverse-proof); numbers still route through the structured channel. **Degrade honest:** provider failure /
> no provider / malformed reply → fall back to the deterministic behavior (original query), never fabricate.
> Contract: `src/ragspine/agent/CLAUDE.md`. *Follow-up:* HyDE's dense-on maximization (W1 `auto`); the
> deterministic zero-LLM step-back; per-transform recall-lift A/B on the W5 harness.

### W10 — RAPTOR + chunking strategies ⭐  (P2)

> **✅ SHIPPED (all opt-in / default-off; the default `DefaultChunker` flat index + the default retrieval loop
> stay byte-identical — no default-path file changed).** Three new capabilities on the existing seams,
> benchmarked against **LlamaIndex RAPTOR pack / `SentenceWindowNodeParser` / `SemanticSplitterNodeParser` ·
> RAGFlow RAPTOR**. (1) **RAPTOR** (`retrieval/raptor.py`): `build_raptor_tree` drops RESTRICTED at the door,
> builds leaves from chunks, then recurses with **deterministic threshold clustering** (`cluster_by_similarity`
> — cosine≥τ edges + union-find connected components, the W7b `detect_communities` idiom, **zero randomness**,
> so same chunks + same embedder ⇒ byte-identical tree) + a per-cluster **`is_synthesis=True` summary** (never a
> citable fact; numbers stay structured; the LLM summarizer forbids numbers) carrying the **union of its members'
> provenance** (`⊆` leaf lineage, never fabricated). `RaptorSummarizer` seam: a deterministic zero-LLM
> `ExtractiveRaptorSummarizer` default (this also lands the "deterministic extractive cluster-summary" follow-up)
> + an opt-in `LLMRaptorSummarizer` (`[llm]`, degrades to extractive on provider failure). `RaptorTree.retrieve`
> is collapsed-tree multi-granularity (leaf **or** theme); `RaptorRetriever` is an opt-in `NarrativeRetriever`
> wrapper; `make_raptor_summarizer` / `make_raptor_retriever` + `RAGSPINE_RAPTOR*`, default `none` returns base
> unchanged. **Isolation** is frozen with an honest reverse-proof. (2) **Sentence-window**
> (`SentenceWindowChunker`): one chunk per sentence + a `window_text` (a new optional, default-`""` `Chunk`
> field — equality-safe) for synthesis-time expansion. (3) **Semantic** (`SemanticChunker`): embedding-distance
> boundary splits, reusing `chunk_document` per segment; default zero-dep deterministic lexical-hash embedder
> (offline), real ONNX (`[embed-onnx]`) injectable. Both chunkers ride the existing `make_chunker` /
> `RAGSPINE_CHUNKER` seam and inherit the provenance conformance pack (`CHUNKER_IMPLS` grew to four). No new
> dependency (stdlib union-find + math cosine + existing embedders). Contracts:
> `src/ragspine/retrieval/docs/raptor.md` + `src/ragspine/retrieval/docs/chunker.md`. *Follow-up:* UMAP+GMM
> soft-clustering opt-in adapter; collapsed-tree vs tree-traversal A/B; retrieval-time window/parent expansion;
> sub-paragraph semantic boundaries; threading synthesis nodes into `answer_question` behind citation suppression.

**Gap:** chunking has W4b's layout / parent-child (opt-in) but **no multi-granularity tree** — no way to retrieve
at theme level for global / thematic synthesis questions. **RAPTOR** (Recursive Abstractive Processing for
Tree-Organized Retrieval, Sarthi et al. 2024) is the other mainstream global-synthesis route besides W7b
narrative GraphRAG. And beyond W4b there is no **sentence-window** or **semantic** (embedding-boundary)
chunking.

**Deliverable:**

- **RAPTOR multi-granularity tree** — recursively cluster chunks (UMAP + GMM as in the paper, or a
  **deterministic clustering** default: agglomerative / connected-components over the embedding graph),
  LLM-summarize each cluster, recurse → a tree whose nodes span fine detail → broad theme; retrieval can pull a
  leaf (detail) **or** an internal node (theme), filling the global / multi-hop synthesis gap as a **second
  route parallel to W7b**. **Clustering deterministic**; **summaries are `is_synthesis=True`, never citable as
  fact** (reuses the W5 / W7b anti-fabrication discipline); **every node carries provenance** (its leaf chunks'
  `source_doc_id` + locators). Behind `[llm]` (+ optional clustering extra), default-off. Benchmarks LlamaIndex
  RAPTOR pack, RAGFlow RAPTOR.
- **Sentence-window chunking** — index single sentences, expand to a ±N-sentence window at synthesis time
  (precise retrieval, rich context). Benchmarks LlamaIndex `SentenceWindowNodeParser`.
- **Semantic chunking** — split on embedding-similarity boundaries (consecutive sentences whose embedding
  distance spikes start a new chunk) rather than fixed-char. Behind `[embed-onnx]`. Benchmarks LlamaIndex
  `SemanticSplitterNodeParser`.

All three ride the **existing `Chunker` seam** (`make_chunker` / `RAGSPINE_CHUNKER`), opt-in; the default
`DefaultChunker` flat index stays **byte-identical**. RAPTOR is the heaviest (LLM summaries + clustering, behind
extras); sentence-window is light; semantic needs `[embed-onnx]`. Benchmarks **LlamaIndex RAPTOR pack /
`SentenceWindowNodeParser` / `SemanticSplitterNodeParser` · RAGFlow RAPTOR**.

*Follow-up:* RAPTOR collapsed-tree vs tree-traversal retrieval modes; incremental tree updates on re-ingest; a
**deterministic extractive cluster-summary** (zero-LLM RAPTOR variant, summaries still labeled syntheses) so a
determinism-only deployment still gets multi-granularity; an A/B on global-synthesis golden cases.

### W11 — Retrieval representation upgrade ⭐  (P2, heavy)

**Gap:** retrieval is single-vector dense (W1 ONNX MiniLM) + BM25 → RRF. No **late-interaction / multi-vector**
(ColBERT-style token-level MaxSim, a precision tier above single-vector dense) and no **learned-sparse** (SPLADE
neural sparse, stronger than BM25 and still interpretable).

**Deliverable:** two optional retrieval backends on the **existing `EmbeddingBackend` / `VectorStore` seams**
(or a new **multi-vector seam** where single-vector cosine doesn't express the score):

- **ColBERT / late-interaction** — token-level multi-vector embeddings scored by **MaxSim** late interaction
  (sum over query tokens of max similarity to any doc token). Offline-first via **fastembed**'s
  `LateInteractionTextEmbedding` (`colbert-ir/colbertv2.0`, Apache-2.0) or onnx, behind `[colbert]`. Needs a
  **multi-vector index / store seam** (single-vector cosine `VectorStore` can't express MaxSim). Usable as a
  **retriever** *or* a **reranker** (the W2 chain). Benchmarks Weaviate / Vespa / Jina ColBERT, LlamaIndex
  `ColbertIndex` / `ColbertRerank`.
- **SPLADE / learned-sparse** — neural sparse term-expansion vectors (interpretable like BM25, stronger).
  Offline via fastembed `SparseTextEmbedding` (`prithivida/Splade_PP_en_v1`) / onnx, behind `[splade]`; fits a
  sparse-vector store. Benchmarks Vespa SPLADE, the 2025 SPLADE-v3 frontier.

**Heavy**: multi-vector indexes (N vectors / doc), model weights, "first-pull-then-offline". fastembed
(Apache-2.0) keeps it offline-first and ADR-0009-clean. **Default hybrid (W1) unchanged** — these are opt-in
backends behind extras, selected by config. **Inherits** the isolation conformance (RESTRICTED never surfaces) +
provenance. Benchmarks **Weaviate / Vespa / Jina ColBERT · Vespa SPLADE · LlamaIndex `ColbertIndex` /
`ColbertRerank` · the 2025 ColBERTv2 / SPLADE-v3 frontier**.

*Follow-up:* a multi-vector `VectorStore` adapter (PLAID / Vespa-style index) for scale; a ColBERT-as-reranker
(W2 chain) vs ColBERT-as-retriever A/B; storage-cost honesty (multi-vector indexes are large).

### W12 — ColPali visual-document retrieval ⭐  (P2, heaviest)

**Gap:** the family OCR→text route (W3a) loses page layout / figures when a question depends on visual structure
(charts, dense financial tables, figure placement). There is **no vision-document retrieval** — embedding the
page *as an image* and doing late interaction directly on it, **with no OCR→text step**. **ColPali / ColQwen2**
(Faysse et al. 2024) is the mainstream route here, often markedly stronger on chart / figure-dense financial
reports — a strong route **parallel to** (not replacing) W3a's offline OCR→text.

**Deliverable:** an optional **page-as-image visual retriever** — render each page to an image, embed with
**ColPali / ColQwen2** (a vision-language late-interaction model, e.g. fastembed
`LateInteractionMultimodalEmbedding` / `vidore/colpali-v1.2` / `vidore/colqwen2-v0.1`), score by **MaxSim over
patch embeddings** directly on the *image* (no OCR→text), preserving layout / chart / figure visual structure.
**Reuses the W11 multi-vector / late-interaction seam** (it is late interaction over image patches instead of
text tokens). Behind `[colpali]` (+ `[llm]` / vision). **Needs a GPU + a vision model** — **honestly annotated**:
GPU dependency + first-pull weight download, **opt-in, default-off, never on the lean / CPU default path**.
Offered **alongside** the W3a family-OCR→text route (both available; visual retrieval wins on chart-dense docs,
OCR→text wins on offline / deterministic / CPU). Benchmarks **LlamaIndex ColPali · Weaviate / Vespa ColPali ·
the 2025 ColPali / ColQwen frontier**.

*Follow-up:* a CPU / quantized ColPali path if one matures; **fusing** visual-retrieval hits with the OCR→text
channel (RRF over both routes); honest GPU / throughput benchmarking; ColQwen2 vs ColPali model choice.

## Gap matrix (depth)

Legend: **kind** 🛡/⭐/🔧 · **status** ✅ have · ◐ partial · ✗ gap.

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| Default embedding | lexical-hash (non-semantic), dense **off** | ONNX multilingual-MiniLM default (`[embed-onnx]`), dense **on** via `auto` | ⭐ | ✅ | W1 · P0 |
| Rerank offline default | identity pass-through (LLM-only brain) | local cross-encoder (ONNX) | ⭐ | ✅ | W2 · P1 |
| OCR default + scanned path | GPU PaddleOCR-VL; **scanned never OCR'd** | family OCR (pdfspine→ocrspine) default + scanned path wired | 🛡⭐ | ✅ | W3a · P0 |
| `.docx` ingestion | **no path** | `docspine` Extractor (tables→facts + paragraphs→chunks) | ⭐ | ✅ | W3b · P1 |
| PPTX richness | `python-pptx` (color/chart/note) | `pptspine` (richer merges) opt-in; default stays `python-pptx` | ⭐ | ✅ | W3c · P1 |
| Table richness in IR | docx/ppt fills `→None`; nested tables warned-and-dropped | family `fill→resolved_rgb` (SME-gated color path); nested → independent `StyledGrid` (no IR schema change) | ⭐ | ✅ | W3d · P1 |
| Contextual retrieval | bare paragraph; context sidecar-only | deterministic context header + LLM adapter | ⭐ | ✅ (LLM adapter = seam) | W4a · P1 |
| Chunking | fixed-char paragraph-greedy | family-layout + parent-child | ⭐ | ◐ (layout+parent-child opt-in; richer family struct follow-up) | W4b · P1 |
| Faithfulness / groundedness eval | **unmeasured** (citation-match only) | claim-level entailment gate + free-text accuracy | 🛡 | ✅ (offline lexical-entailment default + free-text accuracy; ONNX-NLI / LLM-judge / context-precision-recall = follow-up) | W5 · P1 |
| Multi-hop / decomposition | deterministic Cartesian only | LLM decomposition (opt-in) | ⭐ | ✅ (opt-in fan-out; per-sub-q guard+gate; det. synthesis, LLM-synth = follow-up) | W6a · P2 |
| Corrective retrieval | one filter-drop retry | CRAG grade→act loop (opt-in) | ⭐ | ✅ (bounded ≤2 det. grade→act; lexical grader default, CE/LLM grader = seam) | W6b · P2 |
| Conversational memory | stateless single-shot | multi-turn (opt-in) | ⭐ | ◐ (bounded memory + det. carry-forward + per-turn gate; LLM coref / endpoint = follow-up) | W6c · P2 |
| Structured relation graph | none (substrate exists) | deterministic typed graph + multi-hop | ⭐ | ✅ | W7a · P2 |
| Narrative GraphRAG | none | entity/community (opt-in, provenance-bound) | ⭐ | ◐ (extract→community→summary skeleton, fake-LLM-tested; Leiden/incremental/global-query = follow-up) | W7b · P2 |
| Graph store seam | none | `GraphStore` Protocol + in-proc default + adapters | 🔧 | ✅ | W7c · P2 |
| Post-retrieval postprocessor | reranked top-k → prompt (no chain) | MMR de-dup + lost-in-the-middle reorder + context compression (det. default · LLMLingua-2 opt-in) — vs LlamaIndex `LongContextReorder`/`MMRPostprocessor`/`SentenceEmbeddingOptimizer` · Haystack `LostInTheMiddleRanker`/`DiversityRanker` · LangChain `ContextualCompressionRetriever` | ⭐ | ✅ (det. MMR + lost-in-the-middle + extractive compression on a `NodePostprocessor` chain, opt-in / byte-identical; LLMLingua-2 / LLM compression = seam-only follow-up) | W8 · P1 |
| Query transformation | det. synonym multi-query + W6a decomposition only | HyDE + RAG-Fusion + step-back + Adaptive-RAG (opt-in LLM) — vs LlamaIndex `HyDEQueryTransform`/`QueryFusionRetriever` · LangChain `MultiQueryRetriever`/HyDE · LangGraph adaptive-rag | ⭐ | ✅ (all four opt-in / byte-identical; HyDE probe-never-a-fact, RAG-Fusion reuses `rrf_fuse`, per-variant security gate, Adaptive reuses `decomposer=` seam; det. step-back / dense-on / A/B = follow-up) | W9 · P2 |
| Multi-granularity tree + chunking | flat index; W4b layout/parent-child only | RAPTOR recursive-cluster tree (det. cluster + `is_synthesis` summaries) + sentence-window + semantic chunking — vs LlamaIndex RAPTOR pack/`SentenceWindowNodeParser`/`SemanticSplitterNodeParser` · RAGFlow RAPTOR | ⭐ | ✅ (det. threshold-clustering tree + `is_synthesis`/never-fabricated-provenance summaries + sentence-window/semantic on the `Chunker` seam, all opt-in / byte-identical; UMAP+GMM cluster / tree-traversal mode / retrieval-time expansion = follow-up) | W10 · P2 |
| Retrieval representation | single-vector dense + BM25 → RRF | ColBERT late-interaction (multi-vector MaxSim) + SPLADE learned-sparse, offline via fastembed — vs Weaviate/Vespa/Jina ColBERT · Vespa SPLADE · LlamaIndex `ColbertIndex`/`ColbertRerank` | ⭐ | ✗ | W11 · P2 |
| Visual-document retrieval | OCR→text only (W3a) | ColPali/ColQwen2 page-as-image late interaction (GPU, opt-in) — vs LlamaIndex ColPali · Weaviate/Vespa ColPali · 2025 ColQwen | ⭐ | ✗ | W12 · P2 |

## Phasing

- **P0 — make the default correct (no new model risk to the lean path).**
  - **W1** ✅ real semantic embedding default + dense-on via `auto` (pure BM25 stays the zero-dep
    fallback; lean path byte-identical). Shipped — see the W1 SHIPPED note above.
  - **W3a** `ocrspine` default OCR + **wire the scanned-PDF path** (pure plumbing, zero charter tension).
  - Re-baseline retrieval A/B with the real default; add it to the CI ratchet.
- **P1 — the depth that wins evaluations.**
  - **W2** ✅ local cross-encoder reranker (shipped — see the W2 SHIPPED note above);
    **W3b/W3c/W3d** docspine/pptspine extractors + richer IR;
    **W4** ✅ contextual retrieval (W4a) + ◐ family-layout/parent-child chunking (W4b, opt-in — see the W4 SHIPPED
    notes above); **W5** ✅ the groundedness eval gate (faithfulness + free-text answer-accuracy, offline
    deterministic default — see the W5 SHIPPED note above).
  - **W8** ✅ post-retrieval postprocessor chain (the competitor-benchmark batch's P1 item — see the W8 SHIPPED
    note above) — MMR de-dup + lost-in-the-middle reorder (both deterministic, zero-model) + extractive context
    compression after the W2 cross-encoder; LLMLingua-2 / LLM compression opt-in. Ships opt-in to keep the loop
    byte-identical.
- **P2 — reasoning depth & governance.**
  - **W7a** structured relation graph (charter-native multi-hop) → **W7c** `GraphStore` seam →
    **W7b** opt-in narrative GraphRAG; **W6** ✅/◐ agentic depth (W6a decomposition ✅ · W6b CRAG ✅ · W6c
    multi-turn ◐), all opt-in, default-off — the deterministic default loop and its byte-identical eval unchanged.
  - **W9** ✅ LLM query transforms (HyDE / RAG-Fusion / step-back / Adaptive-RAG) on the `QueryRewriter` /
    `IntentParser` seam — all opt-in / default-off, the default loop byte-identical (see the W9 SHIPPED note above).
  - **W10** ✅ RAPTOR multi-granularity tree (det. threshold-clustering + `is_synthesis` summaries) +
    sentence-window / semantic chunking on the `Chunker` seam — all opt-in / default-off, the default loop
    byte-identical (see the W10 SHIPPED note above). **W11–W12** ✗ the rest of the competitor-benchmark batch,
    all opt-in / default-off behind extras: **W11** ColBERT late-interaction + SPLADE learned-sparse retrieval backends (heavy,
    multi-vector seam); **W12** ColPali visual-document retrieval (heaviest, GPU-gated). The deterministic
    default loop + its byte-identical eval stay unchanged.

Each piece follows the ADR 0005 promotion rule: experimental adapter until it has a real, CI-tested,
conformance-bound path, then "core/supported."

## User stories

1. As a user, `pip install ragspine[embed]` gives me a **genuinely semantic hybrid default** (BM25 + bge-small →
   RRF) with no config — and `pip install ragspine` (no extras) still runs offline on the lexical-hash fallback.
2. As an operator who can't run GPUs, scanned PDFs are **actually OCR'd offline** by `ocrspine` and become
   retrievable — instead of silently sitting in a review queue.
3. As a user with Word reports, `.docx` ingests with first-class tables (gridSpan/vMerge/nested) and cell-level
   citations — a format that has no path today.
4. As a user, retrieval reranks by meaning **offline** (local cross-encoder), not only when I wire a cloud LLM.
5. As a quality owner, CI **measures faithfulness** — an answer that adds a claim not entailed by its snippets
   fails a ratcheted gate, so "anti-fabrication" is proven on the narrative side, not just asserted.
6. As an analyst, I ask *"how did <entity> do against its peers across regions, and what drove it?"* — the
   structured relation graph rolls up subsidiaries and walks the competitor edges (deterministic, fully cited),
   then the narrative channel attributes the why.
7. As a security-minded operator, the new graph + rerank + OCR backends **inherit the isolation/provenance
   conformance packs** — a `RESTRICTED`-sourced node never surfaces in a traversal, the same way it can't surface
   in retrieval.
8. As a LlamaIndex / Haystack user, I get the same **node-postprocessor** stage — MMR de-dup, lost-in-the-middle
   reorder, context compression — but the deterministic processors keep the default loop **byte-identical and
   offline** (W8), and I opt into LLMLingua-2 compression only when I want it.
9. As a user who knows **HyDE / RAPTOR / ColBERT / ColPali** from the mainstream frameworks, I can opt into each
   (LLM query transforms, a multi-granularity tree, late-interaction / learned-sparse retrieval, visual-document
   retrieval) — yet `pip install ragspine` stays a deterministic, offline, byte-identical default loop (W9–W12),
   and the LLM / vision pieces are default-off behind extras.
10. As a security-minded operator, even the new postprocessor / query-transform / multi-vector / visual backends
    **inherit the isolation + provenance conformance packs** — RESTRICTED never surfaces through an MMR reorder, a
    fused sub-query, or a ColBERT / ColPali hit, and HyDE's hypothetical doc is never a citable fact.

## Implementation decisions

- **Fix the default before adding adapters.** W1/W3a change the *default* loop's quality; everything else extends
  it. A semantic default + wired OCR is worth more than ten optional backends.
- **Family producers are owned, not rented.** ocrspine/docspine/pptspine adapters live behind extras like any
  adapter, but they are first-party, offline, deterministic, and invariant-clean — they are the moat, so they get
  the ⭐ treatment (conformance + eval), not the thin-wrapper treatment.
- **Determinism is preserved by phasing.** Every non-deterministic addition (LLM decomposition, narrative
  GraphRAG, LLM context) is **opt-in, default-off**; the byte-identical default loop and its eval are unchanged.
- **Anti-fabrication is never traded for graph or context.** Numbers stay in the structured channel; graph edges
  and chunk-context headers carry provenance and are never citable facts; W5 makes the narrative guard measurable.
- **Permissive-license-only, lazy, extra-gated** for every model-bearing piece (ONNX embedder/reranker/NLI, graph
  libs) — the ADR 0009 license gate extends to the new extras (`[embed]`, `[rerank]`, `[eval]`, `[graph]`).
- **Reuse the breadth contract** for the new seams (`GraphStore`, the cross-encoder reranker): Protocol · offline
  default · thin adapter · registry · conformance. No new extension style.

## Testing decisions (TDD — write these red first)

- **Eval gate: groundedness (W5).** A golden case whose answer adds an un-entailed claim **fails** the new
  faithfulness gate; a faithful answer passes. Free-text narrative accuracy is scored, not just numeric.
- **Eval ratchet: real-embedding A/B (W1).** Hybrid-with-bge beats BM25-only on the real golden set by a
  baselined margin; the deterministic-hash disclaimer is removed.
- **Determinism conformance (W1/W2).** The ONNX embedder and cross-encoder yield byte-identical outputs across
  two runs; pinned model + opset.
- **Scanned-path wiring (W3a).** A scanned-PDF fixture ingested through the default pipeline yields retrievable
  chunks/facts via `ocrspine` (not a review-queue stub); low-confidence cells still route to review.
- **Isolation conformance, extended (W2/W7c).** A `RESTRICTED` candidate fed to the cross-encoder reranker is
  never emitted; a `RESTRICTED`-sourced node is never returned by a `GraphStore` traversal. Reverse-proof stubs
  fail the pack.
- **Provenance conformance, extended (W3b/W3c/W7).** Every `StyledGrid` cell from docspine/pptspine, and every
  graph node/edge, carries non-null `source_doc_id` + locator; a lineage-dropping stub fails.
- **Lean-default smoke (W1).** With **no** extras installed, the full pipeline runs offline on BM25 +
  lexical-hash + identity rerank — adapters are never on the default path.
- **Graph multi-hop (W7a).** A peer-comparison / subsidiary-roll-up query returns the correct cited set on a
  fixture profile; a derivation-trace walks the `derived_from` edges deterministically.
- **Byte-identity under opt-in (W8–W12).** With no postprocessor / query-transform / RAPTOR-chunker / multi-vector
  backend selected (`make_postprocessor`/`make_query_transform`/`make_chunker`/`EmbeddingBackend` defaults), the
  full pipeline is **byte-identical** to today; the determinism golden + lean smoke stay green. MMR + lost-in-the-
  middle are deterministic, byte-identical across two runs (zero model).
- **Isolation conformance, extended (W8/W11/W12).** A `RESTRICTED` candidate fed through the MMR / lost-in-the-
  middle / compression postprocessor chain (W8), a fused / step-back sub-query (W9), or a ColBERT / ColPali hit
  (W11/W12) is **never emitted**; HyDE's hypothetical doc never becomes a citable fact. Reverse-proof stubs fail.

## Out of scope (v1 of this PRD)

- **A general graph database / Cypher surface.** W7c ships one offline default + one or two adapters behind the
  conformance kit, not a query-language engine.
- **Training or fine-tuning embedders/rerankers/NLI models — or any SOTA retrieval / vision model.** We ship
  *pinned, permissive, pre-trained* models as offline defaults / opt-in backends (ONNX embedder/reranker/NLI,
  and the W11/W12 ColBERT / SPLADE / ColPali / LLMLingua weights); authoring or fine-tuning a SOTA model is out.
- **GPU as a default dependency.** W12 ColPali (and any vision-document route) needs a GPU + a vision model; it
  is **opt-in, default-off, honestly annotated, never on the lean / CPU default path**. The CPU-offline
  deterministic loop remains the product; the heavy W11 multi-vector / W12 visual backends are extensions over
  it, not a new default.
- **Real-time / streaming ingestion and cross-store incremental sync.** Lineage-correct deletion-propagation
  remains the breadth PRD's P2 🛡 concern.
- **Full conversational agent / tool-marketplace.** W6 adds bounded, opt-in multi-hop + memory, not an
  open-ended agent framework (ADR 0009 forbids orchestration lock-in).
- **Replacing the deterministic default with an LLM-first loop.** The deterministic, offline, anti-fabrication
  default is the product; every LLM-powered depth feature is an opt-in extension over it.

### Follow-ups (carried out of shipped work)

- **First-run-offline embedding weights (from W1).** `OnnxEmbeddingBackend` (via fastembed) is
  "first-pull-then-offline": it downloads the ONNX weights from HuggingFace on first use, then caches.
  A truly first-run-offline default would ship the pinned weights as a **data-pack** (the
  `ocrspine-models` pattern) so a fresh, network-less install is semantic out of the box. Deferred —
  W1 ships the "first-pull-then-offline + deterministic" real-semantic default; the data-pack is a
  packaging follow-up, not a code-path change.
- **First-run-offline + A/B measurement for the reranker (from W2).** `CrossEncoderReranker` (via
  fastembed `TextCrossEncoder`) shares W1's "first-pull-then-offline" weight download — the same
  data-pack follow-up applies. Separately, W2 ships the reranker + wiring + determinism/isolation
  conformance but **not** a ratcheted A/B quantifying its precision lift over identity/RRF; that
  eval-delta lands with the W5 groundedness/eval gate (a depth item isn't "done" until the eval
  ratchet shows it improved the answer).
- **Agentic-depth follow-ups (from W6).** W6 ships all three pieces **opt-in, default-off** (the deterministic
  default loop + its byte-identical eval are unchanged). Carried: (W6a) an LLM **synthesis pass over the
  sub-answers** — today the decomposition synthesis is deterministic concatenation (each sub-answer already
  guarded); plus HyDE / planning. (W6b) the **cross-encoder / LLM `RelevanceGrader`** behind the seam — today the
  grade is the offline deterministic lexical-overlap proxy. (W6c) **true LLM coreference / pronoun resolution** and
  a **multi-turn FastAPI endpoint** — today the carry-forward is a conservative deterministic slot fill and the
  session is programmatic, not yet endpoint-wired.
- **Real entailment model + the richer groundedness metrics (from W5).** W5 ships the **offline
  deterministic** faithfulness + free-text answer-accuracy gates on a *lexical-overlap* entailment proxy
  (default, CI-green). Deferred behind the `EntailmentJudge` seam (`make_entailment_judge`): the real
  **ONNX-NLI judge** (`[eval]`, a small permissive MNLI / cross-encoder NLI — `@pytest.mark.network`
  weight pull, the W1/W2 "first-pull-then-offline" pattern) and the **LLM-judge** (`[llm]`, opt-in
  default-off). Also deferred: **context-precision / context-recall / answer-relevance**, **composite-case
  narrative-segment faithfulness**, and the **W1/W2 real-embedding retrieval A/B** ratchet on this harness.

## Further notes

- This PRD is the answer to "is RAGSpine *accurate*, not just *broad*?": **yes — by owning the ⭐ stages
  (semantic default, local rerank, family extraction, contextual/parent-child chunking), measuring the 🛡
  invariant (groundedness) where it actually fails, and reasoning over a graph that stays deterministic and
  cited.** Breadth is renting the surface; depth is the part the conformance kit and the eval ratchet make
  *true*.
- The depth gap matrix is the canonical quality backlog (companion to the breadth capability matrix). A stage
  moves ✗→◐→✅ as its default, adapter, conformance, and **eval delta** land — a depth item isn't done until the
  eval ratchet shows it improved the answer.
- The compound moat is the whole spine family in one offline, deterministic, invariant-clean pipeline:
  pdf/ppt/doc → ocrspine OCR → structured + narrative → semantic hybrid → cross-encoder → grounded answer →
  graph multi-hop. No breadth framework can rent that, because no breadth framework owns the producers.
