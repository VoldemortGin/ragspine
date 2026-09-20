# ADR 0011: Document catalog, service mount and the verified natural-language answer chain

Status: Accepted, 2026-09-20. Offline-verified; one round of real-model acceptance (18 cases,
AIA release + an authored PDF, real Qwen3 embedder / reranker and a real answer model) is
recorded in [`CLAUDE_HANDOFF.md`](../CLAUDE_HANDOFF.md) with its open issues, not restated here.

## Context

ADR 0010 and its update gave every PDF an explicit `ingest → qualify → index → publish`
lifecycle, but a published draft only existed on disk. The HTTP app factory still hard-wired the
AIA store roots, `POST /v1/chat/completions` only did source review, and there was no way to
select a document. Handoff items 2 and 3 asked for two things: mount any published document
behind a document-selecting service, and answer natural-language questions over the evidence
chain by reusing `ragspine` retrieval pieces instead of writing a second retriever — while
keeping immutable snapshots, corrupt-evidence refusal, no implicit model call, credential
isolation, and the family's anti-fabrication / provenance invariants.

Constraints that shaped the design: `ragspine` may be imported only under `adapters/`; the pure
packages guarded by `check_architecture.py` are stdlib-only; the AIA runtime under
`data/output/aia-2026-interim` must not be moved, cleaned or overwritten; the default gate never
calls a provider; and `TableIR` / `TableCell.verification` are pinned `PENDING` by construction in
`processing/table_models.py`, so a "verified grid" cannot exist without a new qualification rule.

## Decision

1. **Catalog and mount** (`adapters/document_catalog.py`). `scan_catalog(ingestion_root,
   legacy_roots=)` discovers `<sha256>/{source,processing}` directories and reads their
   `current-*` pointers into `CatalogEntry` values (`retrieval_status` `ready` / `not_indexed` /
   `corrupt` plus a reason; pinned processing id, retrieval snapshot id, embedding fingerprint,
   member count, label, pages). `mount_document(entry, *, embedder)` opens exactly that entry's
   two stores, reloads the pinned manifest, re-runs `validate_processing_source`, and refuses on
   any mismatch — including an embedder whose fingerprint differs from the published index —
   before any model call. `MountedDocument` (the Protocol in `answers/ports.py`) exposes
   `manifest / search / resolve / member_texts / chart_context / displayed_context`; every
   request re-reads the pinned manifest and refuses drift. `embedder=None` mounts evidence only:
   `resolve` works, `search` raises `QueryEmbeddingUnavailable`. `mount_catalog` records
   per-document failures instead of hiding or raising them.

2. **`document-catalog` execution mode** (`core/settings.py`, `adapters/http/app.py`).
   `APP_EXECUTION_MODE=document-catalog` serves the catalog under `APP_INGESTION_DIR` (default
   `data/ingestion`). `APP_LEGACY_DOCUMENT_ROOTS` is a JSON list of processing-store roots whose
   parent is the source store, so the AIA release is served in place; it defaults to empty.
   `aia-source-review` is untouched and the two modes coexist. The factory builds each provider
   once from its own environment group — `EMBEDDING_*` → `LocalEmbeddingAdapter` shared by every
   mount, `OPENAI_*` → one `JsonCompletionClient` (cache `<ingestion_root>/model-cache`, live
   budget `APP_ANSWER_MAX_LIVE_CALLS`, default 200), `RERANK_*` → `LocalRerankJudge`. A missing
   group yields `None`, which is a 503 on the routes that need it and never a mock. Without
   `EMBEDDING_*` a document is still `mounted=True` with `embedding_configured=false`: evidence
   reads work, search is 503.

3. **`document-catalog-v1` contract** (`adapters/http/documents.py`, `catalog_schemas.py`,
   `docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`). `GET /v1/documents` lists every
   entry with `mounted` / `mount_error`, `embedding_configured`, `embedding_fingerprint` and
   `unpublished` directories; `GET /v1/documents/{id}` adds the processing status (or `null`
   when unmounted); `GET .../manifest` returns the pinned release; `POST .../search` and
   `POST .../context` are the per-document versions of the existing processing search and
   evidence hydration. Unknown id → 404; visible but unmounted → 409 with the reason; drifted or
   corrupt evidence → 409; embedder missing or failed → 503 with fixed wording that carries no
   credential, provider body or path.

4. **Hybrid retrieval** (`adapters/hybrid_search.py`). The vector channel is the mount's pinned
   cosine index; the lexical channel is BM25 over the same embedded description texts
   (`LexicalIndex`, content-addressed by snapshot id and scoring parameters, cached per
   process); fusion is reciprocal rank fusion. Exactly
   `ragspine.retrieval.lexical.retrieval.{tokenize, bm25_scores, rrf_fuse}` and
   `ragspine.retrieval.rerank.listwise_rerank.{ListwiseJudge, listwise_rerank}` are reused.
   Rerank is opt-in per request through an injected judge and off by default; a request that
   asks for it without a configured judge is a dependency error, not a silent skip.

5. **One model call, then verification** (`processing/context_builder.py`, `answers/`,
   `adapters/answer_service.py`, `adapters/json_completion.py::complete_text_json`). Each
   resolved hit becomes a `ContextBlock` whose `prompt_text()` prints the only citable paths
   (`fragments.<span_id>`, `cells.<cell_id>`, `points.<point_id>.value`); `budget_blocks` drops
   whole blocks, never truncates one. `answers/prompt.py` fixes `SYSTEM_RULES` and the strict
   `ModelAnswer` schema (≤ 16 claims, `extra="forbid"`). `AnswerService.answer` calls the model
   at most once per request through `complete_text_json` (own fingerprint salt
   `bounded-text-json-v1`, optional system prompt, same cache and budget as the vision path).
   `answers/verify.py` then re-reads every claim from stored evidence: a quote must be a verbatim
   (whitespace-folded, case-folded) substring of its span; a cell must equal the stored
   `PRESENT` cell text; a chart value must equal the qualified `Decimal` or its exact source
   display, requalified through `check_context` / `check_fields` for donuts and
   `chart_context` + `check_displayed_evidence` for displayed bars, and is cited back to SVG
   elements. Nothing derives, rounds or combines a value. `decide` applies, in order: model
   abstain → `MODEL_DECLINED`; failed claims are dropped one by one into `rejected`; zero
   verified claims → abstain with the first rejection's reason, else `NO_VERIFIED_CLAIM`; any
   number in the prose that is not a verified claim's number → the whole answer abstains with
   `CLAIM_NOT_IN_EVIDENCE`; otherwise `ANSWERED`. `AnswerResult` carries the document sha256,
   processing id, snapshot id, member ids, fused hits, request fingerprint, live call count and
   cache hit for every outcome.

6. **`rag-chat-v1` contract** (`adapters/http/chat.py`, `chat_schemas.py`,
   `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`). `GET /v1/models` lists one model per
   mounted document, `enterprise-pdf-rag/<sha256[:12]>`. `POST /v1/chat/completions` selects the
   document by `document` (a sha256 or a prefix of at least twelve hex digits) over a model id
   of that shape over catalog uniqueness; the last message must be `user`; earlier `user` /
   `assistant` turns are forwarded as data; client `system` messages are dropped. The response
   is a standard `chat.completion` whose `content` is the answer followed by a numbered `引用:`
   list, plus an `enterprise_pdf_rag` envelope (status, abstain reason and detail, provenance
   ids, claims with field-level citations, rejected claims, `llm_live_calls`, `cache_hit`).
   Abstention is a 200 business result. `stream=true` finishes retrieval, the model call and
   verification before the first byte, replays the text as SSE, and sends a trailer chunk with
   empty `choices` and the envelope before `[DONE]`. Status codes: 404 unknown document; 409
   unmounted, drifted or corrupt evidence, `ChartQueryError` `INVALID_EVIDENCE` /
   `PIN_CONFLICT`; 422 ambiguous or missing selection, request invariants, non-user last
   message; 503 answer model / query embedder / reranker unconfigured or failed,
   `ChartQueryError` `UNAVAILABLE_EVIDENCE`.

7. **Table members** (`processing/table_transcription.py`, `adapters/source_objects.py`,
   `semantic_objects.py`, `literal_qualification.py`, `processing_retrieval.py`). A Table is
   retrievable only when its **literal transcription** is `VERIFIED` under the same standard as
   Text / List / Group: `ObjectDescription.verification == VERIFIED`, producer
   `exact-source-transcription-v1`, a `LiteralQualification` receipt and a `SUCCEEDED`
   qualification stage. The inferred grid stays `PENDING`. `table_span_ids` and
   `check_table_transcription` (grid inside the anchor, every span inside its cell, `PRESENT`
   text equal to its spans' text in span order modulo whitespace, `BLANK` / `UNAVAILABLE` cells
   hiding no span) are shared by the producer and the validator, so a table admitted at
   processing time re-verifies at index and resolve time. When transcription fails the IR is kept
   for review and the description / qualification stages are `UNAVAILABLE` with the reason.
   `eligibility()` admits `TABLE` and names the non-verified case. The policy strings move to
   `source-transcription-and-scoped-chart-qualification-v2` (part of the retrieval snapshot id)
   and `retrieval-eligibility-kind-and-stage-completeness-v2`; they are informational — no code
   refuses a snapshot for its policy string, so snapshots built under v1 keep their ids and stay
   mountable. Context blocks report the description's verification for tables; cell claims
   verify against `PRESENT` cell text only.

8. **`answers/` is a pure package.** It joins `check_architecture.PACKAGES`; pydantic is its
   only third-party allowance (`EXTRA_ALLOWED`) because the model's strict output schema lives
   there. `FusedHit` lives in `answers/models.py` so `AnswerResult` can carry it without importing
   an adapter, and `adapters/hybrid_search.py` re-exports it.

## Rejected alternatives

- **Symlinking or moving the AIA release into `data/ingestion/<sha256>/`.** It would touch the
  protected runtime the handoff forbids cleaning, symlinks need privileges on Windows, and the
  hard-link semantics under `objects/sha256` are not controlled after a move. A legacy-root
  setting serves the release where it is.
- **Re-implementing retrieval in this package, or importing `HybridRetriever` and the
  `ragspine` agent wholesale.** The pinned cosine index, evidence resolvers and refusal rules
  already exist here; only the pure ranking functions and the listwise rerank orchestration were
  missing. `ragspine`'s agent chain assumes its own structured / narrative channels and
  providers, so wiring it would have meant reproducing this package's evidence model inside it.
- **Retrying the model when a claim fails verification.** It hides fabrication behind repeated
  sampling, spends the live budget, and lets a second answer differ from the fingerprinted first
  one. Failed claims are dropped and the prose gate decides; the rejections stay visible.
- **A `VERIFIED` `TableIR`.** The grid's rows, columns and merges are inferred and pinned
  `PENDING`; verifying them is a new qualification with no source rule. Verifying the literal
  transcription is what makes a cell citation always resolve to real spans.
- **Mounting `/v1/documents*` and the RAG chat inside the AIA app.** That app is bound to one
  store root and one model id. A second mode keeps the acceptance profile byte-identical and
  makes the catalog explicit.
- **A mock answer model when `OPENAI_*` is missing.** Chat is 503; the default gate keeps
  running with scripted clients in tests only.

## Consequences and follow-ups

- Offline coverage: `tests/enterprise_pdf_rag/adapters/test_document_catalog.py`,
  `test_documents_http.py`, `test_hybrid_search.py`, `test_chat_http.py`,
  `tests/enterprise_pdf_rag/answers/` (store-backed `MountedDocument` bridge, scripted
  `JsonCompletionClient`), `processing/test_context_builder.py`,
  `processing/test_table_transcription.py`, and the generic e2e / draft publication / PDF
  ingestion suites extended with an authored table page. The suite uses authored PDFs, the
  `OfflineDescriptionEmbedder` and scripted model output; no real provider is called.
- `adapters/http/webui_gate.py` still admits only the AIA source-review and offline-demo model
  ids, so Open WebUI does not front `document-catalog` mode yet.
- `deploy/enterprise-pdf-rag/open-webui/backend.Dockerfile` remains unverified since the merge
  (ADR 0021).
- There is no frozen gold set for natural-language answers; the chart-QA golds are the only
  frozen sets. Building one (text, table and chart questions with positives, refusals and
  corrupt-evidence cases) is a prerequisite for claiming generic QA quality.
- Real acceptance — a real local-embedder `index`, a real answer model, the AIA release mounted
  through `APP_LEGACY_DOCUMENT_ROOTS` — ran once on 2026-09-20 (evidence under
  `data/validation/generic-chat-2026-09-20/`, results in the handoff): no number or fact outside
  the verified evidence reached an answered response. It is one round, not a frozen gold set.
- **Geometry tolerance (BUG-1, fixed the same day).** The prompt renders canonical bboxes at
  full float precision while models return the shortest decimal, so strict `<=` containment
  checks failed by ~6e-15 on real calls and never in the offline stub. `processing/geometry.py`
  (`COORDINATE_TOLERANCE = 1e-6`, `contains(outer, inner, *, tolerance)`) now backs every
  "model outer box ⊇ canonical inner box" comparison, including the table transcription and
  literal qualification checks of Decision 7; canonical-vs-canonical checks, `table_models.py`,
  `pdfspine_tables.py` and the chart geometry are untouched. The offline stub rounds to six
  decimals so the suite reproduces the real shape.
- **ISSUE-2, chart recall.** Chart members embed only a short description, so the lexical and
  vector channels favour long text: the page-18 donut was not in the top-6 for a question that
  named the chart but not its categories, with or without rerank. Candidate directions — add
  period / category aliases to chart descriptions, or weight chart members on the query side —
  are undecided.
- **ISSUE-3, the prose gate treated years as numbers — resolved (rag-spine 0.14.0).** "in 1H
  2026 was 17.5%" abstained because `2026` was not inside a verified claim's text. The gate
  (`answers/verify.py::prose_grounded`) now grounds a prose number when it (a) equals a verified
  claim's text number or value, (b) appears verbatim in the user's question
  (`AnswerRequest.question`), or (c) equals a number in the evidence text the verified claims
  cite — the span quote, the table cell text, or a chart claim's period / category labels and
  source display (`ClaimCitation.quote`). Any other number still abstains the whole answer, and
  zero verified claims are handled exactly as before; `decide` keeps its order.
- `AnswerEnvelope` does not carry `request_fingerprint`, so a failed live call cannot be located
  under `model-cache/requests/<fingerprint>.json` from the response alone.
- The four `contains` call sites in `adapters/visual_semantics.py` have no dedicated regression
  test. The layout stage of `ingest` costs one real model call per selected page, text-only pages
  included, by design.
- `JsonCompletionClient` runs with `retry_failed=False`: a failed live call is cached and
  replayed as the same failure; deleting `<ingestion_root>/model-cache/requests/<fingerprint>.json`
  is the only way to retry, and it is deliberate.
- Page-20 ChartQA v2 stays an independent acceptance under ADR 0009; the answer chain reads
  displayed-bar evidence but does not activate it.
- The policy `-v2` strings only change snapshots built from now on; existing releases (the AIA
  `a7384f0c…` processing with policy `source-transcription-and-numeric-paint-qualification-v1`)
  remain `ready`.
