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
   cosine index; the lexical channel is BM25 over the same embedded index texts
   ([ADR 0012](0012-chart-index-text-and-retrieval-seats.md); a description text for every member
   except a chart with citable values, which is projected) — `LexicalIndex`, content-addressed by
   snapshot id and scoring parameters, cached per process; fusion is reciprocal rank fusion.
   Exactly
   `ragspine.retrieval.lexical.retrieval.{tokenize, bm25_scores, rrf_fuse}` and
   `ragspine.retrieval.rerank.listwise_rerank.{ListwiseJudge, listwise_rerank}` are reused.
   Rerank is opt-in per request through an injected judge and off by default; a request that
   asks for it without a configured judge is a dependency error, not a silent skip.
   Amended by [ADR 0018](0018-query-classification-and-translation.md): fusion is no longer
   unconditional. A short label-and-period question is answered from BM25 alone and a question
   the lexical channel cannot score from the vector channel alone; the mode is reported in the
   envelope, and the query embedder became a dependency of the requests that read it rather
   than of the route.

5. **One model call, then verification** (`processing/context_builder.py`, `answers/`,
   `adapters/answer_service.py`, `adapters/json_completion.py::complete_text_json`). Each
   resolved hit becomes a `ContextBlock` whose `prompt_text()` prints the only citable paths
   (`fragments.<span_id>`, `cells.<cell_id>`, `points.<point_id>.value`); `budget_blocks` drops
   whole blocks, never truncates one. `answers/prompt.py` fixes `SYSTEM_RULES` and the strict
   `ModelAnswer` schema (≤ 16 claims, `extra="forbid"`). `AnswerService.answer` calls the model
   at most once per request through `complete_text_json` (own fingerprint salt
   `bounded-text-json-v1`, optional system prompt, same cache and budget as the vision path).
   Amended by [ADR 0018](0018-query-classification-and-translation.md): one *synthesis* call
   per request, plus at most one earlier translation call (task salt `query-translation-v1`)
   for a question written outside the index's language — bounded and cached the same way, and
   skipped whenever it is unavailable. Amendments 1 and 3 of that ADR fix where its output may
   go: the **lexical** channel — BM25 and the mode classifier — and the period / region
   pre-filters, whose derivation is unioned with the question's own. The vector channel and the
   rerank judge keep the question as asked; measured on one Chinese question, the asker's own
   wording ranked the target 12th by vector where its translation ranked it 32nd, a difference
   of phrasing rather than of language (the translation wrote `agents'` where the index prints
   `Agency`). The prompt, the prose number gate and claim verification never see a translation.
   `answers/verify.py` then re-reads every claim from stored evidence: a quote must be a verbatim
   (whitespace-folded, case-folded) substring of its span; a cell must equal the stored
   `PRESENT` cell text; a chart value is read as a number plus the unit printed around it — the
   number must be that point's exact source display, or failing that the qualified `Decimal`, and
   a unit the claim carries must be the point's own `unit.text` verbatim (see "A chart claim's
   unit is read before its number" below) — requalified through `check_context` / `check_fields`
   for donuts and `chart_context` + `check_displayed_evidence` for displayed bars, and cited back
   to SVG elements. Nothing derives, rounds, normalises or combines a value. `decide` applies, in
   order: model abstain → `MODEL_DECLINED`; failed claims are dropped one by one into `rejected`;
   zero verified claims → abstain with the first rejection's reason, else `NO_VERIFIED_CLAIM`; any
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
  Superseded for ruled tables by [ADR 0014](0014-ruled-table-grid-proof.md), which supplies the
  source rule.
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
- **A frozen gold set for natural-language answers now exists** (2026-09-20):
  `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json`, 25 cases pinned to
  one published release, registered in that folder's `manifest.json`. Its schema, self-checks
  and single pass/fail rule live in `adapters/nl_gold.py`, and two runners share that rule —
  `tests/enterprise_pdf_rag/answers/test_nl_gold.py` (offline replay of the pinned evidence,
  in the default gate) and `scripts/enterprise_pdf_rag/nl_gold_eval.py` (live service, run
  before a release). Covered: verbatim text quotes, chart values, diagram nodes, English /
  Chinese / keyword / title-only phrasings, opt-in rerank, derived and explicit pre-filters, a
  relaxed impossible filter, the completion cache, five abstentions (a forecast, a
  cross-period subtraction, an order no edge draws, content outside the selected pages, a
  headcount nobody printed) and three adversarial probes that script an illegal model output.
  Table-cell questions and corrupt-evidence cases are **not** in it yet; they remain follow-ups,
  as does widening it beyond this one document.
- Real acceptance — a real local-embedder `index`, a real answer model, the AIA release mounted
  through `APP_LEGACY_DOCUMENT_ROOTS` — ran once on 2026-09-20 (evidence under
  `data/validation/generic-chat-2026-09-20/`, results in the handoff): no number or fact outside
  the verified evidence reached an answered response. That round is what the frozen gold set
  above was built from; it is now repeatable by one command instead of by hand.
- **Geometry tolerance (BUG-1, fixed the same day).** The prompt renders canonical bboxes at
  full float precision while models return the shortest decimal, so strict `<=` containment
  checks failed by ~6e-15 on real calls and never in the offline stub. `processing/geometry.py`
  (`COORDINATE_TOLERANCE = 1e-6`, `contains(outer, inner, *, tolerance)`) now backs every
  "model outer box ⊇ canonical inner box" comparison, including the table transcription and
  literal qualification checks of Decision 7; canonical-vs-canonical checks, `table_models.py`,
  `pdfspine_tables.py` and the chart geometry are untouched. The offline stub rounds to six
  decimals so the suite reproduces the real shape.
- **ISSUE-2, chart recall — resolved by [ADR 0012](0012-chart-index-text-and-retrieval-seats.md).**
  The attribution recorded here first ("chart members embed only a short description, so the
  channels favour long text") is wrong and is corrected there. Measured, the cause was four
  specific things: a chart's index text was its *title alone* (`Distribution Mix`, two tokens), so
  BM25 ranked the page-18 donut 1st while the vector channel left it 13–20th; RRF with `k = 60`
  caps a single-channel hit at `1/61`, below any ordinary paragraph both channels find;
  `channel_limit = 20` sat on that vector rank and `top_k = 6` cut the resulting fused rank ≥ 7
  (the cached prompt contained zero chart blocks); and rerank scored a concatenation of those same
  index texts instead of the evidence blocks. ADR 0012 indexes charts by a projection of their
  qualified IR, widens the defaults to 10 / 50, feeds the reranker evidence blocks and reserves one
  conditional seat for a citable chart.
- **ISSUE-3, the prose gate treated years as numbers — resolved (rag-spine 0.14.0).** "in 1H
  2026 was 17.5%" abstained because `2026` was not inside a verified claim's text. The gate
  (`answers/verify.py::prose_grounded`) now grounds a prose number when it (a) equals a verified
  claim's text number or value, (b) appears verbatim in the user's question
  (`AnswerRequest.question`), or (c) equals a number in the evidence text the verified claims
  cite — the span quote, the table cell text, or a chart claim's period / category labels and
  source display (`ClaimCitation.quote`). Any other number still abstains the whole answer, and
  zero verified claims are handled exactly as before; `decide` keeps its order.
- **A chart claim's unit is read before its number — fixed 2026-09-21.** `prompt.SYSTEM_RULES`
  asks a chart claim for "the displayed value with its unit", a rule written for a figure that
  prints its own `%`. A `$m` figure prints the unit in its caption (`VONB ($m)`) and the bare
  number on the bar, so the exact source display of a p.13 point is `294`: the model obeyed the
  prompt, wrote `294$m`, and the verifier compared that against `294` and rejected it. The prompt
  and the verifier contradicted each other, and a real gold case abstained on it every run.
  `SYSTEM_RULES` is deliberately *not* changed — editing it invalidates every cached completion
  and forces the whole real-model gold set to be re-run — so the verifier is the side that moved.
  `answers/verify.py` gains `_CLAIMED_NUMBER_RE` (the number inside a claimed display: `294$m`,
  `294 $m`, `$294m`, `$294 m`, `8.2%`, `1,168 $m`, `-294 $m`, and the accounting bracket `(294)`,
  which is how a figure prints a sign and so stays with the number), `_split_unit` (whatever sits
  on either side of that number, joined in reading order, is the claimed unit; a text holding no
  number comes back whole with no unit and is compared exactly as before) and `_display_mismatch`,
  which `_value_claim` now calls. Only the unit moves: `294.0` still does not equal `294` and
  `1,168` keeps its separator, so a split can never turn one figure into another, and a claim
  carrying no unit is judged on its number alone — everything that verified before still verifies.
  The fix also closes a real hole. Before it, `294%` against a `$m` point was *accepted*: the
  literal comparison failed, the numeric fallback ran, `_decimal("294%")` stripped the sign and
  read `Decimal("294")`, and a claim contradicting the figure's own unit verified. A unit the
  point does not print is now a rejection in its own right, `AbstainReason.UNIT_MISMATCH`, saying
  either "claimed unit X is not the point's unit Y" or "claimed unit X but the point prints no
  unit". That member was declared with the enum and never raised by anything — the chart-QA
  services refuse on their own `RefusalReason` / `DisplayedRefusalReason`, which merely spell the
  same name — so the claim re-read is its first caller. Since `decide` abstains with the first
  rejection's reason when nothing verifies, `unit_mismatch` becomes a reachable `abstain_reason`
  on `rag-chat-v1` for the first time; the enum itself is unchanged. Offline
  coverage: the new `tests/enterprise_pdf_rag/answers/test_verify_claim_units.py` (29) beside
  `answers/test_verify.py` (30).
- **What that fix exposed, and whose fault it is not.** Citing p.13's `$m` bars for the first time
  turned a safe refusal into a *wrong sourced number*. That page prints three `VONB ($m)` charts
  side by side — AIA Thailand 514, AIA Singapore 294, AIA Malaysia 232 — while region metadata is
  page-level ([ADR 0013](0013-page-metadata-and-prefilters.md)), so every member on it carries
  every one of those country values and no pre-filter can tell the columns apart. A Thailand
  question consequently answers with Singapore's or Malaysia's figure, cited verbatim, with a real
  bbox behind it. That is not an error of this fix: the old unit rejection was an accidental
  barrier standing in front of the wrong answer, not a check against it. The repair is a
  member-level, in-column region binding, which does not exist yet; until it does the `k01` /
  `k02` gold cases stay frozen as `abstained` known gaps rather than record today's behaviour. The
  retrieval half of the same run is in [ADR 0018](0018-query-classification-and-translation.md).
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
