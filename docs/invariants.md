---
covers:
  - src/ragspine/agent/agent.py
  - src/ragspine/agent/number_guard.py
  - src/ragspine/retrieval/link/
  - src/ragspine/retrieval/rerank/
  - src/ragspine/common/observability/
verified-against: 6ebaaa4c3340f5dfcb69c255c94b3b6bc4724c74
---

# Invariants (code-enforced)

Authoritative detail for the invariants summarized in the root `CLAUDE.md`. Each
entry should name: what it guarantees · where it is enforced · the test that
freezes it.

## Anti-fabrication

**Guarantees** a number in an answer comes from evidence: a structured `found` fact, or (on the
narrative channel, fallback included) a number that appears in the retrieved snippet text. With no such evidence the answer
is the deterministic not-found / unrecognized rewrite (for a missing metric: not-found followed by
the `ask_first` metric question, or just that question when no fallback ran), never model prose.

**Enforced** in `agent/agent.py`. `_structured_answer` renders `found` facts from the fact value
(model prose discarded) and rewrites no-`found` to not-found / unrecognized. **Route fallback (ADR 0023,
`RAGSPINE_NARRATIVE_FALLBACK=on|off`, default `on`, needs an injected narrative retriever):** a
structured-route question with a missing metric or no `found` fact first tries `_run_narrative(fallback=True)`.
The result is accepted only when `_fallback_grounded` holds: snippets were retrieved, the answer has no
`NO_ANSWER` sentinel, and at least one answer number (minus question numbers and `[n]` markers) appears
in the snippet text. Sources are forced as on the narrative route. Otherwise a no-hit returns the
original structured result unchanged, and a missing metric answers "查不到：…" plus the original ask
(the `ask_first` clarification object is kept). The competitor / out-of-scope refusal stays the first early return;
`found`, composite and narrative routes never fall back. The request trace records
`narrative_fallback={reason, grounded}` (codes only).
**Narrative number guard (ADR 0024, `RAGSPINE_NARRATIVE_NUMBER_GUARD=on|off`, default `on`):** every
`_run_narrative` synthesis (narrative route, composite attribution, accepted fallback) goes through
`agent/number_guard.guard_narrative_answer`. Each answer number must appear in the snippet text under the
nl-gold normalization (`common/answer_text.normalize_answer` / `contains_normalized`; magnitude amounts compared
by value, `%` never matches a bare number). Exempt: question numbers, source doc / locator strings, `[n]`
markers, page / slide refs, list numbers, and years / periods that match the question or snippets. An ungrounded
number in the lead ⇒ `NUMBER_GUARD_NOTICE` + the original sentences that hold only grounded numbers. Elsewhere ⇒
only those sentences are dropped, plus a trailing note. No second LLM call. Sources are still forced. When on, the
system prompt also carries `NUMBER_GUARD_RULE` (no calculation / order / causal inference). Off ⇒ byte-identical.
Trace: `narrative_number_guard={ungrounded, rewritten}` (counts only, only on rewrite).
**Frozen by** `tests/agent/test_agent_orchestrator.py` (not-found / unrecognized rewrite,
`test_found_path_discards_fabricated_extra_number`), `tests/agent/test_narrative_fallback.py`
(fallback grounded / ungrounded / fabricated-number rejected / off ≡ old behavior),
`tests/agent/test_narrative_number_guard.py` (computed number rewritten, raw / format-variant / question /
citation / period numbers pass, sources kept, off ≡ snapshot), and the QA ratchet
(`data/golden/qa_baseline.json`, fabrication count 0).

## Provenance

<!-- TODO: source_doc_id + locator carried end to end; where lineage could be dropped. -->

## RESTRICTED isolation (two exits)

**Guarantees** `sensitivity == RESTRICTED` content never reaches an LLM prompt or an answer.
**Enforced** at two exits before any prompt: `retrieval/link/narrative_link.py` (the snippet
adapter drops RESTRICTED chunks) and `retrieval/rerank/listwise_rerank.py` (RESTRICTED text never
enters the listwise judge prompt). Both must stay; neither is sufficient alone.

**Parent-child window expansion rides the same exit (ADR 0018).** The small-to-big `window_text` →
`prompt_text` swap in `narrative_link._to_snippet` runs **after** the `link/` exit has dropped RESTRICTED
chunks, so a RESTRICTED chunk's whole snippet — parent window included — is rejected (整段拒绝) and the
parent context can never leak via a child. The expanded window is generation-context only; `text` /
`source_locator` stay the hit child, so citation stays honest and the window is never a hit. **Frozen by**
`tests/conformance/test_parent_child_isolation.py` (end-to-end + a RESTRICTED-windowed reverse-proof).

**Page-level parent/child (`RAGSPINE_PAGE_PARENT=off|dedup|page+child`, service / facade default `page+child`).** `NarrativeIndex`
de-duplicates the fused ranking by page (`{doc_id}@page=N`) before rerank and swaps the representative's
`window_text` for its whole page, surfaced through the same `_to_snippet` → `prompt_text` path. RESTRICTED chunks
never join a page group (`page_parent/pages.group_pages`): they stay single units that the two exits drop as before,
the page window (`page_parent/window.page_window`) and the `page+child` whole-page BM25 unit are built from
non-RESTRICTED siblings only. `text` / `source_locator` stay the hit chunk; `parent_locator` is the page locator.
**Frozen by** `tests/conformance/test_page_parent_isolation.py` (+ reverse-proof) and the off-mode byte snapshot
`tests/retrieval/page_parent/test_page_parent_off_snapshot.py`.

**Headings in the index text (`RAGSPINE_CONTEXTUAL_INDEX=off|heading|full`, default `off`).** The heading path / W4a
header only enters the BM25 / vector **index text**; snippets (`text`, `prompt_text`) are unchanged. The `page+child`
whole-page unit's heading comes from `group_pages` members only, so a RESTRICTED chunk's heading never reaches a
page unit, the judge or a prompt; RESTRICTED chunks are still never embedded. **Frozen by**
`tests/retrieval/contextual_index/test_contextual_index.py` (restricted-heading cases) and the off-mode snapshot
`tests/retrieval/contextual_index/test_contextual_index_off_snapshot.py`.

**Query translation only reaches retrieval (`RAGSPINE_QUERY_TRANSLATION=off|auto`, default `auto`).** The translated
question is an extra BM25 / vector query inside `NarrativeIndex.retrieve`; the rerank judge, the prompt, the
anti-fabrication rewrite and citations all keep the original question, and retrieval output is still real chunks
filtered at the same two RESTRICTED exits. The trace (`op=narrative.query_translation`) carries language / reason codes
and counts, never the question or the translation. **Frozen by**
`tests/retrieval/query_translation/test_query_translation.py` (generation-prompt isolation, trace privacy) and the
off-mode snapshot `tests/retrieval/query_translation/test_query_translation_off_snapshot.py`.

**Page images are a new exit, screened at the door (`RAGSPINE_PAGE_IMAGES=all|tagged`, `on` = `all`, opt-in, default `off`).** A page
image carries the whole page, wider than any chunk the two text exits judge. So (1) ingest never renders a page that
holds any RESTRICTED chunk (`ingestion/page_images/index.py`, `n_withheld`), and (2) `retrieval/page_images/attach.py`
re-checks the chunk store at query time and sends no image for such a page even if a mapping row exists (e.g. the
sensitivity changed after rendering). Public text on that page still flows through the normal exits. Traces record
counts / reason codes only (`op=narrative.page_images`, `op=narrative.page_image_index`, request-trace `page_images`),
never paths or image bytes; each image part carries `doc_id` + physical `page` (provenance). **Frozen by**
`tests/conformance/test_page_image_isolation.py` (+ reverse-proof) and the off-mode prompt snapshot
`tests/retrieval/page_images/test_page_images_off_snapshot.py`. **The trigger only removes, never adds**
([ADR 0025](adr/0025-page-image-trigger-policy.md)): `retrieval/page_images/trigger/retriever.py` wraps the image
exit and can only delete `page_image` keys (tagged filter, de-dup, `max` cap) — never add a reference or touch another
key — so the door screening above is inherited unchanged; the conformance test runs `tagged` next to `on`.

**Judge-agnostic (W2).** The rerank exit's protection lives in the `listwise_rerank` *orchestration*,
not in any particular judge, so it covers **every** `ListwiseJudge` equally: the LLM listwise judge
(`ProviderListwiseJudge`) and the offline local cross-encoder (`retrieval/rerank/cross_encoder.py`,
`CrossEncoderReranker`) both receive only the non-RESTRICTED subset. Adding the cross-encoder seam
therefore inherits — and cannot bypass — the two-exit rule. **Frozen by**
`tests/retrieval/rerank/test_cross_encoder_isolation.py` (RESTRICTED never reaches the cross-encoder;
a reverse-proof shows the assertion has teeth — the same reranker *does* score the text when handed
it directly, bypassing the seam). The HTTP `/v1/rerank` adapter (`retrieval/rerank/scored_judge.py`,
`ScoredRerankJudge`) is one more judge on the same seam — `tests/retrieval/rerank/test_scored_judge.py`
pins that RESTRICTED text never reaches the scoring request.

**At-rest (third, persistence layer).** Persisting a chunk's embedding writes a *recoverable
derivative* of its text next to its lineage (`doc_id`, `source_locator`) — a surface that bypasses
both exits. The swappable `PersistencePolicy`
(`src/ragspine/retrieval/vector/persistence_policy.py`) gates this: the default `IsolationFirstPolicy`
**never persists a RESTRICTED chunk's vector at rest** (it still retrieves via BM25 with vector
score 0, then is stripped at the two exits). `PersistEverythingPolicy` is opt-in and **only**
appropriate when the entire vector store is itself classified RESTRICTED-tier at rest — encrypted
volume, access-controlled, and excluded from routine backups. The `where`-filter pushdown in
`VectorStore` (`retrieval/vector/store.py`) is an additional optional enforcement point at the store.
The opt-in persisted chunk index (`retrieval/vector/chunk_index.py`, service switch `persist_vectors`)
syncs through the same policy — RESTRICTED chunks are counted as `withheld`, never embedded
(`tests/service/test_persistent_vector_index.py`).

**Frozen by** `tests/retrieval/lexical/test_persistence_ingest.py` (default policy persists zero
RESTRICTED vectors; opt-in persists them) and the existing two-exit tests under
`tests/retrieval/link/` and `tests/retrieval/rerank/`.

**Inherited by the W6 opt-in agentic paths (decomposition / CRAG / multi-turn).** All three W6 features are
opt-in, default-off, and re-use the existing exits rather than opening a new one: W6a query decomposition
(`agent/decompose.py`) re-runs the **full** `answer_question` per sub-question, so the security gate + two
exits screen every sub-question independently (a competitor sub-question is still out-of-scope-refused); W6b
corrective retrieval (`retrieval/corrective.py`, `CorrectiveRetriever`) only ever returns a *subset* of its
wrapped base retriever's already-RESTRICTED-stripped output and never reads chunks directly (frozen by
`tests/retrieval/corrective/test_corrective_isolation.py` + reverse-proof); W6c conversational memory
(`service/conversation.py`) re-screens the augmented question through the gate every turn and never carries
home context into an out-of-scope question or remembers a refused turn (frozen by
`tests/service/test_conversation.py`). None can leak RESTRICTED / bypass competitor refusal by construction.

**Inherited by the W7 GraphRAG paths (graph traversal is a new exit).** Graph traversal is a new path that could
reach a prompt, so the `GraphStore` (`src/ragspine/graph/store.py`) screens it like the two exits: a
`sensitivity == RESTRICTED` node never surfaces in `get_node` / `neighbors` / `subgraph` / `traverse`, and never
acts as a multi-hop stepping-stone (edges touching it never appear); doc nodes inherit `sensitivity` from their
source chunk (most-restrictive wins). The W7a multi-hop `GraphQuery` (`graph/query.py`) additionally screens every
requested entity through the deterministic `SecurityGate` **first**, so a competitor/external entity is refused
with zero data — graph queries cannot smuggle competitor data or RESTRICTED content. **Frozen by**
`tests/conformance/test_graph_store.py` (provenance / RESTRICTED-never-surfaces / determinism, each with an honest
reverse-proof stub — `_LeakyGraphStore` / `_LineageDroppingGraphStore` — that must FAIL) and the W7a/W7b tests
under `tests/graph/`. The authoritative per-domain contract is `src/ragspine/graph/CLAUDE.md`.

**Inherited by the W8 post-retrieval postprocessor chain (opt-in, no new exit).** The W8 chain
(`retrieval/postprocess.py`: `MMRPostprocessor` / `LostInTheMiddlePostprocessor` / `CompressionPostprocessor`,
composed by `make_postprocessor`) runs *inside* `NarrativeIndexRetriever.retrieve` **after** the `link/` exit has
already stripped `sensitivity == RESTRICTED`, and only ever reorders / de-dups / compresses that already-stripped
subset — it never reads chunks directly and never fabricates a snippet, so RESTRICTED can neither enter a
processor nor surface (the W6b `CorrectiveRetriever` idiom). Default `"none"` ⇒ no chain ⇒ retrieval output is
byte-identical. Compression preserves provenance by writing a separate `prompt_text` key (`agent._snippet_text`
prefers it for the prompt) while leaving the original `text` + every reference field (`source_locator` / `doc_id`
/ `chunk_id`) untouched — the W4a index_text layering. **Frozen by**
`tests/retrieval/postprocess/test_postprocess_isolation.py` (real-index integration: a RESTRICTED chunk never
surfaces through the `mmr,lost_in_middle,compress` chain, with a **reverse-proof** that a RESTRICTED snippet fed
*directly* passes through — proving the protection lives at the upstream exit, not the postprocessor). The
authoritative per-domain contract is `src/ragspine/retrieval/docs/postprocess.md`.

**Inherited by the W9 query transforms (opt-in, no new exit).** The W9 LLM query transforms
(`agent/query_transform.py`: `HyDERetriever` / `RAGFusionRetriever` / `StepBackRetriever`, composed by
`make_query_transform`) are `NarrativeRetriever` wrappers (the W6b `CorrectiveRetriever` idiom): each only ever
calls `base.retrieve(...)` — whose `link/` exit has already stripped `sensitivity == RESTRICTED` — and only
reorders / fuses (via `rrf_fuse`) that already-stripped subset, so RESTRICTED can neither enter a transform nor
surface. Two further guarantees make the transforms anti-fabrication-safe: (1) **HyDE's hypothetical document is
a retrieval probe, never a citable fact** — it replaces only the *query text* fed to `base.retrieve`; the
returned snippets are the real chunks with real lineage, and the hypothetical doc never enters a snippet /
answer / citation. (2) **Every LLM-generated variant / step-back question re-runs the deterministic
`SecurityGate` before retrieval** — a competitor / out-of-scope generated query is dropped and never retrieved
(the W6a idiom; the original question already passed the gate at `answer_question` entry, so this screens only
the *newly introduced* queries). Default `"none"` ⇒ `make_query_transform` returns the base unchanged ⇒ the
retriever path is byte-identical (and `answer_question` itself is untouched — Adaptive-RAG reuses the existing
`decomposer=` seam via `AdaptiveDecomposer`, `multi` → W6a fan-out, `simple`/`single` → the byte-identical
single-shot route). **Frozen by** `tests/agent/test_query_transform.py` (HyDE probe-never-a-fact; RAG-Fusion RRF
fusion; per-variant / per-step-back competitor screening with a spy-base **reverse-proof**; a real-index
isolation integration test with a RESTRICTED-in-store reverse-proof; factory byte-identity). The authoritative
per-domain contract is `src/ragspine/agent/CLAUDE.md`.

**Inherited by the W10 RAPTOR tree + chunking strategies (opt-in, isolation-at-the-door + no new default exit).**
RAPTOR (`retrieval/raptor.py`) is the second global-synthesis route (parallel to W7b) and, like it, is
opt-in / default-off with three code-enforced disciplines. (1) **Isolation at the door:** `build_raptor_tree`
drops every `sensitivity == RESTRICTED` chunk **before** any leaf / summary / provenance list is built, so
RESTRICTED can never enter the tree, a summary, or a citation — frozen by
`tests/retrieval/test_raptor.py::test_restricted_never_enters_tree` with an honest reverse-proof
(`test_restricted_exclusion_reverse_proof`: the same chunk marked `INTERNAL` *does* enter). The
`RaptorRetriever` wrapper inherits it twice — the base already stripped RESTRICTED at the `link/` exit, and the
tree was built without it. (2) **Summaries are syntheses, never citable facts:** every internal node is
`is_synthesis=True` (the LLM summarizer prompt forbids concrete numbers, mirroring W7b); numbers stay in the
structured channel. (3) **Provenance never fabricated:** each summary's `source_doc_id` / `source_locator` is
the sorted union of its members' lineage and is `⊆` the leaf lineage (frozen by
`test_summary_provenance_never_fabricated`); clustering is deterministic (cosine-threshold union-find, the W7b
`detect_communities` idiom). The two W10 **chunking** strategies (`SentenceWindowChunker` / `SemanticChunker`,
behind the existing `Chunker` seam) inherit the chunker **provenance conformance pack** (`CHUNKER_IMPLS` grew
to include them) and keep the `chunk.text` = original-substring contract; the default `DefaultChunker` flat
index stays **byte-identical**. The authoritative per-domain contracts are
`src/ragspine/retrieval/docs/raptor.md` + `src/ragspine/retrieval/docs/chunker.md`.

**Inherited by the W11 retrieval-representation rerankers (opt-in, no new exit).** The W11 ColBERT
late-interaction reranker (`retrieval/rerank/colbert.py`, `ColbertReranker`, token-level multi-vector
**MaxSim**) and SPLADE learned-sparse reranker (`retrieval/rerank/splade.py`, `SpladeReranker`,
sparse **dot product**) both implement the **existing** `ListwiseJudge` Protocol and run *inside* the
unchanged `listwise_rerank` orchestration — exactly the W2 cross-encoder idiom. So the rerank exit's
protection (RESTRICTED candidates excluded from any judge, kept in RRF position; all-RESTRICTED →
judge not called) covers them without re-implementation: RESTRICTED text never reaches
`LateInteractionTextEmbedding.embed` / `SparseTextEmbedding.embed`. Both land as **rerankers** (not
retrieval backends), selected by the existing `make_reranker` factory (`colbert` / `splade` specs);
default `ServiceConfig.reranker == "none"` ⇒ `make_reranker` returns `None` ⇒ the judge selection is
byte-identical (multi-vector / sparse indexes for the retriever mode are a follow-up). Provenance is
untouched — a reranker only reorders the candidates the upstream `link/` exit already produced, never
fabricating a snippet or dropping lineage. **Frozen by**
`tests/retrieval/rerank/test_colbert_isolation.py` and `tests/retrieval/rerank/test_splade_isolation.py`
(RESTRICTED never reaches the embed call, kept in position; a **reverse-proof** shows the same reranker
*does* embed/score the text when handed it directly, bypassing the seam — proving the protection lives
at the upstream orchestration). The authoritative per-domain contract is
`src/ragspine/retrieval/docs/late-interaction.md`.

**Inherited by the W12 ColPali visual-document retrieval (a new exit, screened at the door).** Visual retrieval
(`retrieval/vision/colpali.py`, `ColPaliVisualRetriever`) is a **new path that could reach a prompt** — it embeds a
document **page as an image** and does late interaction directly on it (**no OCR→text**), so the text two-exit
(`link/` + `rerank/`) never sees the page. Like W7 `GraphStore` / W10 RAPTOR, it is therefore screened **at the
door**: the retriever drops every `sensitivity == RESTRICTED` page at **index construction**, so a RESTRICTED page
**never** enters the visual index (`self.pages`), is **never** handed to the visual embedder's `embed_images`, and
**never** surfaces in a hit (all-RESTRICTED → empty index → `retrieve` returns `[]`, embedder not called;
case-insensitive, same convention as the two exits). The visual MaxSim scoring **re-uses** `rerank/colbert.maxsim`
(the same function object) and `page→image` **re-uses** `pypdfium2`; the real backend (fastembed
`LateInteractionMultimodalEmbedding`, `[colpali]`, lazy, `@pytest.mark.gpu`) is opt-in / default-off — nothing in
the default loop wires it, so retrieval + `answer_question` stay **byte-identical**. **Anti-fabrication:** a visual
hit is `is_visual=True` with `text=""` (a page-reference retrieval lead, never a citable fact) — numbers stay in the
structured channel and the visual model can never inject a fabricated figure; provenance (`doc_id` / page
`source_locator` / `page_no`) is carried, never fabricated. **Frozen by**
`tests/retrieval/vision/test_colpali_isolation.py` (RESTRICTED never enters the index / an `embed_images` call / the
output, with a **reverse-proof** that the same embedder *does* encode the page when handed it directly — proving the
protection lives at the retriever's door, not the embedder). The authoritative per-domain contract is
`src/ragspine/retrieval/docs/visual-retrieval.md`.

## Privacy-aware traces

**Guarantees** a trace payload carrying an answer / fact value / chunk text is **rejected (or scrubbed)** —
observability records only codes / counts / timings, never restricted content. No sink — including OTel — can
turn the trace channel into a leak surface.

**Enforced** by construction, not by convention. `common/observability` (a package: `trace.py` +
`sink.py` + `adapters/`) runs every `emit_trace` payload through a corespine `InProcessPrivacyTraceSink`
first — a forbidden content key (`answer` / `value` / `text` / `content` / `prompt` / `completion` / `chunk` /
`chunk_text` / `body`, case-insensitive exact key match against `FORBIDDEN_KEYS`) raises `TraceError` **before
anything is logged**. The default `emit_trace` path is byte-identical to how it has always worked.

**Formalized as a seam (B1).** `common/observability/sink.py` lifts this into a **pluggable, privacy-enforced
`TraceSink` seam** so observability can fan out to OTel/files *through the privacy conformance test, never
around it*. It **reuses** corespine's `@runtime_checkable TraceSink` Protocol + `InProcessPrivacyTraceSink`
default (no duplicate Protocol), adds a `make_trace_sink` / `RAGSPINE_TRACE_SINK` registry with
`ragspine.trace_sinks` entry-point discovery (five-段式范式同 `make_vector_store`), and a reusable privacy
gate `enforce_trace_privacy` that **every** sink calls first. The `OtelTraceSink` adapter
(`observability/adapters/otel.py`, behind `[otel]`, Apache-2.0, lazy-imported) passes the payload through that
gate before any span attribute is set — so the OTel exit cannot leak content either.

**Frozen by** `tests/conformance/test_trace_sink.py` (the privacy-trace conformance pack): every registered
`TraceSink` (`in_process` + OTel) must reject/scrub a payload containing answer / fact value / chunk text, and
two content-leaking reverse-proof stubs — `_LeakyTraceSink` (records the forbidden payload verbatim) and
`_ValueSmugglingTraceSink` (drops the forbidden key but smuggles the value under a benign key) — fed the same
decision-core **must FAIL**, proving the assertion has teeth. Plus the pre-existing
`tests/common/test_observability_resilience.py` (R6–R9: exactly-one trace, no sensitive value leaks,
forbidden-key rejection, byte-identical default paths).
