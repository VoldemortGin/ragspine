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
- **W3c — `pptspine` Extractor (⭐, P1).** Replace `python-pptx` with `pptspine` for the structured + narrative
  PPTX path (richer merges, autoshapes, notes, embedded-image OCR via the same `ocrspine`).
- **W3d — preserve table richness into the IR (⭐, P1).** Extend `StyledGrid`/`StyledCell` so merges/nested/fills
  from pptspine/docspine survive (today pdfspine tables set `resolved_rgb=None` and ppt/doc richness is unreached),
  so cell-level citations and color/structure semantics resolve to page→table→cell across all three formats.

### W4 — Contextual retrieval + family-layout chunking ⭐  (P1, Chunker seam exists)

**Gap:** chunks are bare paragraphs (`chunk.text` is a raw paragraph join); doc context lives only in sidecar
metadata, never indexed. Chunking is fixed-char paragraph-greedy.

- **W4a — Contextual Retrieval (deterministic default + LLM adapter).** Before indexing, prepend a deterministic
  context header to each chunk — `title · entity · period · section-heading` (all already known, controlled-vocab,
  zero fabrication) — so the embedded/lexical text carries situating context (Anthropic's contextual-retrieval
  technique, deterministic variant). An **opt-in** LLM-written context blurb behind `[llm]` is the higher-recall
  adapter, gated by the anti-fabrication discipline (context is metadata, never a citable fact).
- **W4b — Layout-aware + parent-child chunking (the family-unique lever).** A `Chunker` strategy that chunks on
  **structural boundaries from the family extractors** (headings, sections, table edges from pdfspine/docspine),
  plus parent-child / small-to-big retrieval (retrieve small, expand to parent for synthesis). The breadth matrix
  lists `semantic · contextual · parent-child` as `Chunker` P1 strategies; W4 specs them to **exploit family
  layout**, which generic loaders (which see only `to_text()`) cannot.

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

### W6 — Agentic depth: multi-hop & corrective retrieval ⭐ (opt-in, determinism-preserving)  (P2)

**Gap:** single-shot, rule-routed; decomposition is a deterministic Cartesian over explicitly-enumerated axes;
no HyDE / planning / self-RAG / corrective retrieval / multi-turn.

All of W6 ships **opt-in**, default-off, so the deterministic default loop (and its byte-identical eval) is
unchanged — the determinism invariant is preserved by construction.

- **W6a — LLM query decomposition** behind the existing `IntentParser`/`QueryRewriter` seams (ADR 0010 already
  decouples this): real multi-sub-question fan-out for "which region grew fastest and why" class queries.
- **W6b — Corrective retrieval (CRAG) / self-RAG**: relevance-grade retrieved context; on low grade,
  re-retrieve (drop filters / rewrite) or refuse — turning the single `retry_without_filters` fallback into a
  principled grade→act loop, with every action traced.
- **W6c — Conversational memory**: a stateless→multi-turn upgrade (follow-ups, coreference) behind the service
  layer, with the security gate + isolation re-asserted per turn.

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

## Gap matrix (depth)

Legend: **kind** 🛡/⭐/🔧 · **status** ✅ have · ◐ partial · ✗ gap.

| Quality stage | Today | Target | Kind | Status | WS · Phase |
|---|---|---|---|---|---|
| Default embedding | lexical-hash (non-semantic), dense **off** | ONNX multilingual-MiniLM default (`[embed-onnx]`), dense **on** via `auto` | ⭐ | ✅ | W1 · P0 |
| Rerank offline default | identity pass-through (LLM-only brain) | local cross-encoder (ONNX) | ⭐ | ✗ (proto ✅) | W2 · P1 |
| OCR default + scanned path | GPU PaddleOCR-VL; **scanned never OCR'd** | family OCR (pdfspine→ocrspine) default + scanned path wired | 🛡⭐ | ✅ | W3a · P0 |
| `.docx` ingestion | **no path** | `docspine` Extractor | ⭐ | ✗ | W3b · P1 |
| PPTX richness | `python-pptx` (lossy) | `pptspine` (merges/nested/notes) | ⭐ | ◐ | W3c · P1 |
| Contextual retrieval | bare paragraph; context sidecar-only | deterministic context header + LLM adapter | ⭐ | ✗ | W4a · P1 |
| Chunking | fixed-char paragraph-greedy | family-layout + parent-child | ⭐ | ✗ (proto ✅) | W4b · P1 |
| Faithfulness / groundedness eval | **unmeasured** (citation-match only) | claim-level NLI gate + free-text accuracy | 🛡 | ✗ | W5 · P1 |
| Multi-hop / decomposition | deterministic Cartesian only | LLM decomposition (opt-in) | ⭐ | ✗ | W6a · P2 |
| Corrective retrieval | one filter-drop retry | CRAG grade→act loop (opt-in) | ⭐ | ✗ | W6b · P2 |
| Conversational memory | stateless single-shot | multi-turn (opt-in) | ⭐ | ✗ | W6c · P2 |
| Structured relation graph | none (substrate exists) | deterministic typed graph + multi-hop | ⭐ | ✗ | W7a · P2 |
| Narrative GraphRAG | none | entity/community (opt-in, provenance-bound) | ⭐ | ✗ | W7b · P2 |
| Graph store seam | none | `GraphStore` Protocol + in-proc default + adapters | 🔧 | ✗ | W7c · P2 |

## Phasing

- **P0 — make the default correct (no new model risk to the lean path).**
  - **W1** ✅ real semantic embedding default + dense-on via `auto` (pure BM25 stays the zero-dep
    fallback; lean path byte-identical). Shipped — see the W1 SHIPPED note above.
  - **W3a** `ocrspine` default OCR + **wire the scanned-PDF path** (pure plumbing, zero charter tension).
  - Re-baseline retrieval A/B with the real default; add it to the CI ratchet.
- **P1 — the depth that wins evaluations.**
  - **W2** local cross-encoder reranker; **W3b/W3c/W3d** docspine/pptspine extractors + richer IR;
    **W4** contextual retrieval + family-layout/parent-child chunking; **W5** the groundedness eval gate.
- **P2 — reasoning depth & governance.**
  - **W7a** structured relation graph (charter-native multi-hop) → **W7c** `GraphStore` seam →
    **W7b** opt-in narrative GraphRAG; **W6** agentic depth (decomposition / CRAG / multi-turn), all opt-in.

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

## Out of scope (v1 of this PRD)

- **A general graph database / Cypher surface.** W7c ships one offline default + one or two adapters behind the
  conformance kit, not a query-language engine.
- **Training or fine-tuning embedders/rerankers/NLI models.** We ship *pinned, permissive, pre-trained* ONNX
  models as offline defaults; training is out.
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
