# Changelog

All notable changes to RAGSpine are documented here. This project follows Semantic Versioning.

## [Unreleased]

### Added

- **Scaffolding to dissolve `enterprise_pdf_rag` into per-domain `evidence/` subtrees**
  ([ADR 0022](docs/adr/0022-dissolve-enterprise-pdf-rag-into-domain-evidence-subtrees.md),
  proposed). No module moves yet. `enterprise_pdf_rag._moves` freezes the legacy → canonical map
  (139 modules, 8 legacy packages, the AIA sample lane pending); `enterprise_pdf_rag._shim` is a
  meta path finder that, once a module moves, binds its legacy name to the same module object with
  a `DeprecationWarning`; `scripts/enterprise_pdf_rag/rewrite_legacy_imports.py` rewrites imports
  from the same map. The strict ruff set and mypy's `warn_unreachable` / `ignore-without-code` now
  follow `**/evidence/**` and `ragspine.*.evidence.*`, so moved code keeps them.
- **The HTTP narrative route and the narrative worker accept `.md`** (DI markdown). `_NARRATIVE_SUFFIXES`
  in `service/api/routes.py` and `service/tasks/jobs.py` is now `.pptx/.pdf/.md`, so a markdown upload with a
  linked source PDF (sidecar `<stem>.meta.json` `source_pdf`, or the worker payload's `source_pdf`) can run through
  `POST /v1/ingest/narrative/jobs` and under `allowed_upload_root`. The `.md` goes through the same
  `validate_ingest_path` (resolved path inside the root, symlinks included, + suffix); the linked PDF must also
  resolve inside the root, else the job fails with `stage="validation"` before any write. No content sniffing: a
  `.md` is only decoded as UTF-8 text (`errors="replace"`), never executed. Other suffixes are still rejected; the
  structured route does not take `.md`.
- **Cross-lingual query translation (`RAGSPINE_QUERY_TRANSLATION=off|auto`, default `auto`).** When the question's
  language differs from the candidate chunks' (deterministic CJK-character vs Latin-word count; chunk `language`
  metadata when uniform, else text statistics), `retrieval/translation/` asks the LLM provider once per question to
  restate it in the document's language (figures, periods and abbreviations kept; `上半年` -> `1H`, `新业务价值` ->
  `VONB`), cached per translator. The translation is an extra BM25 **and** vector query fused by RRF
  (`HybridRetriever.search(extra_queries=, extra_vector=)`; also fed to the `page+child` whole-page BM25); rerank and
  generation keep the original question. Same-language questions (ACME's Chinese corpus) never call the provider;
  no provider / provider error / empty / unchanged / wrong-language output degrades to no translation, and every
  decision is traced (`op=narrative.query_translation`, codes and counts only, never the question or translation).
  Ported from the enterprise ADR 0018 rules (not imported, ADR 0022). Wired through `ServiceConfig`,
  `RetrievalPreset`, `build_narrative_retriever(query_translation=, translation_provider=)` and the nl-gold eval's
  `--query-translation`; `MockProvider` returns the query unchanged. `off` is byte-identical (frozen by
  `tests/retrieval/query_translation/test_query_translation_off_snapshot.py`). Default `auto` because 71-page
  retrieval (zh bucket, n=10, page+child hybrid) went r@1/5/10 40/50/50% -> 60/90/90% (MRR .42 -> .70; BM25-only
  translation reached 30/80/90%) with no non-zh ranking change, and real-LLM nl-gold v2 (full document, repeat 3) went
  A 86.4% -> 90.9% ±0, B 84.9% ±2.6 -> 90.9% ±0; p06-zh and p11-zh pass 3/3 on both routes.
- **Headings in the index text, opt-in (`RAGSPINE_CONTEXTUAL_INDEX=off|heading|full`, default `off`).**
  `ServiceConfig.contextual_index`, the facade's `RetrievalPreset.contextual_index`,
  `build_narrative_retriever(contextual_index=)`, the worker payload and the nl-gold eval's
  `--contextual-index` wire the existing W4a `index_text_fn` seam: `heading` prefixes `[章节:<heading path>]` to the
  BM25 / vector index text, `full` adds title / entity / period. The prompt text (chunk text or page window) is
  unchanged; a `page+child` whole-page unit carries its page's de-duplicated heading segments once. Persisted
  vectors embed the index text; the doc signature is computed over it, so a sync after a switch re-embeds exactly
  the docs whose index text changed, and the vector db records `contextual_index` (absent = `off`; `migrating:*`
  while a sync runs). The query side raises `VectorIndexMismatchError` on a mismatch instead of mixing vectors.
  `off` is byte-identical (retrieval + vector signature snapshot). Default stays `off`: on the 71-page AIA deck
  `heading` lifts GS BM25 / RRF recall@1 and MRR and keeps the real-LLM nl-gold score (A 86.4%±0, B 86.4%±0 vs
  84.9%±2.6), but route B page recall@1 drops 59% → 53% and probe BM25 recall@5 slips 2–3 points.

### Changed

- **Page-level parent/child now defaults to `page+child`** (was `off`). `RAGSPINE_PAGE_PARENT` /
  `ServiceConfig.page_parent`, the facade's `RetrievalPreset.page_parent` (every profile),
  `build_narrative_retriever(page_parent=)` and the nl-gold eval's `--page-parent` now default to
  `page+child`: the fused ranking is de-duplicated per page, the representative chunk carries its whole
  page as generation context, and a whole-page BM25 ranking is RRF-fused in. Evidence: the retrieval
  ablation; a Sonnet blind review (answerable@1: `page+child` 69% vs `dedup` 46%); and the real-LLM
  nl-gold run on the full document (route B recall@1 .31 → .56, content hit 68% → 84%). Chunks whose
  locator has no `@page=N` (e.g. pptx `slide=`, whole-document PDF chunks) are never grouped, so their
  output is unchanged — the bundled ACME QA eval retrieves byte-identically and its ratchet scores do not
  move. `RAGSPINE_PAGE_PARENT=off` (or `page_parent="off"`) restores the old output byte for byte (frozen
  by the off-mode snapshot); the low-level `NarrativeIndex(page_parent=)` constructor default stays `off`.
  `RAGSPINE_PAGE_IMAGES` stays `off`.
- **The evidence chain's pure extraction packages moved to `ragspine.extraction.evidence`**
  (ADR 0022). `enterprise_pdf_rag.documents` (all but the pending `aia` module),
  `enterprise_pdf_rag.figures` (with `chart_qa`) and the extraction half of
  `enterprise_pdf_rag.processing` now live under `document/`, `figures/`, `page/`, `metadata/`
  and `objects/{tables,diagrams,formulas}/`; the legacy names still import, as the same module
  objects, with a `DeprecationWarning`. **Schema `$defs` rename, payloads unchanged:** in
  `aia-processing-v1` and `document-catalog-v1` (and the FastAPI OpenAPI components) the two
  module-qualified keys `enterprise_pdf_rag__processing__{diagram,formula}_models__PathEvidence`
  are now `ragspine__extraction__evidence__objects__{diagrams__diagram,formulas__formula}_models__PathEvidence`;
  substituting the new key for the old one makes each regenerated schema equal to the old one,
  so no wire payload changes.

## [0.16.1] - 2026-09-22

### Changed

- **The document-tree channel routes by default**
  (`enterprise_pdf_rag`, [ADR 0019](docs/enterprise-pdf-rag/adr/0019-document-tree-channel.md)
  Amendment 1). `ROUTE_BY_DEFAULT` is now `True`: a question against a mounted tree is routed
  unless the request declines it, where 0.16.0 shipped it opt-in. **Not one measurement was
  re-run and no other behavior changed** — `tree_rrf_k` stays 600, the inequality
  `tree_rrf_k + 1 > rrf_k + channel_limit` is still a precondition of `HybridSearch.__init__`,
  and a routing note is still never citable. What changed is which question the same evidence
  answers. 0.16.0 asked whether the channel *earns* its live call and found it did not on a
  twenty-page deck; this asks whether leaving it on can *cost* an answer, and the same arms say
  no from both sides: 22/22 with the channel off and 22/22 with it on, not one verdict moved and
  every case citing exactly the same pages, five structural questions answered 5/5 in both arms,
  and a member only the tree reached scoring `1 / 601` against `1 / 110` for the weakest member
  any scoring channel reached — it cannot outrank one. The price is unchanged and unhidden: one
  extra live call per routed question, +3.8 s to +23.0 s. Three things bound it — a document with
  no tree still has no third channel at all (a deployment that never runs the `tree` stage answers
  byte for byte as before), a short label query is still never routed (ADR 0018's `is_label_query`
  clause, written for this day), and `AnswerRequest.tree_route=False` still returns the answer of a
  treeless service field for field, prompt included. `RagChatRequest` gains no knob, as ever, so
  over HTTP this is simply what the engine now does: a wire question against a mounted tree is
  routed, and the envelope's `tree_route` / `tree_rank` report it. The follow-up stands — this
  sample cannot show the *benefit*, and whether routing raises recall on section-aimed questions,
  and whether 600 is the right weight once a tree has real depth, must still be measured on a
  document of hundreds of pages with a multi-level contents page. That measurement now decides
  whether to keep the default rather than whether to reach it.

### Tests

- **The `pdfspine` producer tag in test fixtures is derived from the installed version**
  (`enterprise_pdf_rag`). `test_source_paint.py` and `formula_observation_fixtures.py` used to
  hardcode the literal `"pdfspine/0.11.0"` as the expected producer tag; both now compute
  `PDFSPINE_TAG = f"pdfspine/{pdfspine.__version__}"` and assert against that, so the suite stops
  drifting out of sync whenever the pinned `pdfspine` dependency is upgraded.

## [0.16.0] - 2026-09-22

### Added

- **A document's own outline answers as a third retrieval channel, and it never testifies**
  (`enterprise_pdf_rag`, [ADR 0019](docs/enterprise-pdf-rag/adr/0019-document-tree-channel.md)).
  BM25 and the vector channel both score fragments, so a question aimed at a named section of a
  long report competed, member by member, with every similarly worded fragment printed anywhere
  else in it: the document's own structure was not an input to retrieval at all. Taking the idea
  from PageIndex (VectifyAI), `processing/document_tree.py` folds a document's table of contents
  into a tree once, at ingestion, and one bounded call per question then picks the sections it
  belongs to. The difference is who writes the structure — here nobody does. The fold is
  deterministic over ADR 0013's verbatim page metadata (a contents page cuts the first level when
  it names at least **2** later pages; dividers and running-header changes cut it otherwise), every
  node's title is a page's own words carrying the `MetadataEvidence` that proved them, and
  "children tile their parent in order" is enforced in `__post_init__` — a tree that loses or
  doubles a page cannot be constructed. The one thing a model writes is a **routing note** per
  non-leaf node (`document-tree-summary-v1`, one text-only call over that node's own page text),
  and it routes rather than testifies: never indexed, never in an answer prompt's evidence blocks,
  never citable. Per question, one cached `document-tree-route-v1` call returns a page set and
  nothing else — at most **6** nodes and **6** pages, resolved deterministically — whose members
  join the same `fuse()` as a third ranking rather than a pre-filter, because a filter can only
  remove and one bad route would hide the answer.

  **That third ranking is weighed at its own RRF constant, and the first measurement is why.**
  Fused as a *peer* of the scoring channels at `k = 60`, the tree made retrieval worse: the frozen
  real-model gold set fell from **21/22 to 17/22**, five cases went pass → FAIL, none of the five
  structural questions written for this feature improved and one stopped answering, and routed
  members held **117 of 220** prompt seats. Asked "Agency share of VONB 1H26" the routed run
  answered **68.3% (ex-Thailand)**, citing two spans that really do print that, instead of the
  **72%** the donut on p18 states — every claim verified, provenance intact, answer wrong. The
  mechanism was arithmetic: a tree rank-1 term of `1/61 = 0.01639` outweighs a member only BM25
  could score at rank 2 (`1/62 = 0.01613`) and outweighs the whole spread of a real top ten
  (0.0044). A page set is not a relevance ranking — the router says where to look, and page order
  inside a section is reading order — so `fuse(..., tree_k=600.0)` now scores it separately, under
  an inequality `HybridSearch.__init__` refuses to run without: `tree_k + 1 > k + channel_limit`,
  which at the service's constants reads `1/601 = 0.00166` against `1/110 = 0.00909`. **A routed
  page may lift a member no channel reached; it may not put one above a member a channel scored.**
  Re-measured the same day on the same release: gold **22/22 with the tree off and 22/22 with it
  on, not one verdict moved and every case citing the same pages**, all five structural questions
  answered in both arms citing the same pages, routed seats down to **78 of 220** of which only
  **4** were reached by the tree alone.

  So the channel ships **opt-in**: `ROUTE_BY_DEFAULT = False`, and a caller asks with
  `AnswerRequest.tree_route=True`. It is provably safe and completely wired, and on a twenty-page
  deck whose two scoring channels already reach every page it changes no answer while costing one
  extra live call and **+3.8 s to +23.0 s** a question — a feature that changes nothing should not
  spend a call per question. PageIndex's premise is documents far longer than this one, and the
  number to beat should be re-measured on a document of hundreds of pages before the default flips.
  `_within_a_channel` still learned `tree_rank`, and after the constant it is the only way the
  channel can seat a member the others missed: such a member scores `1/611 = 0.00164` and sorts
  below every scored seat by construction, so ADR 0012's guaranteed visual seat is what carries it
  into the prompt. `RagChatRequest` gains no knob — the envelope reports `tree_route` and each
  seat's `tree_rank`. The tree is additive: a content-addressed body plus a rewritable record at
  `<processing_root>/document-tree/<processing_id>.json`, never the manifest, so **no index is
  rebuilt and no snapshot id moves** and a release published yesterday gains one by running
  `enterprise-pdf-rag tree`. Measured on the pinned AIA release (**20** pages): `origin = agenda`,
  **22** nodes, **20** leaves, **2** branches, **2** live calls in **12.5 s** cold, and **0** calls
  in **1.1 s** on replay.

- **Every answer is journalled locally, prompt and all** (`enterprise_pdf_rag`).
  `model-cache/contexts/<fingerprint>.json` kept the body one model call carried, but nothing
  kept the shape of a whole answer: which pre-filters narrowed the candidates, what seat each
  channel gave the members that reached the prompt, what the model returned, which claims the
  verifier then dropped. `adapters/answer_audit.py` adds a local SQLite journal — one row per
  question, written twice. `AnswerService` opens the row once the prompt is assembled and
  **before** the transport is touched, so it already holds `prompt_system` and `prompt_user`
  verbatim (byte-identical to `payload.messages` in the request envelope, checked against three
  real answers), the fused ranking with each channel's rank and score, `filters_applied` /
  `filters_relaxed` / `fusion_mode`, the page windows and the seated `member_ids`; it closes the
  same row with `model_output_raw`, `request_fingerprint`, `llm_live_calls`, `cache_hit`,
  `status` / `abstain_reason` / `abstain_detail`, the verified and rejected claims, `answer_text`
  and `elapsed_ms`. The raised path (`DependencyUnavailable`) closes it too, with its error code.
  A journal write never changes an answer: every failure is a warning, and a row that could not
  be opened is never closed. WAL, indexed by `request_fingerprint`, `started_at` and
  `document_sha256`. `Settings.answer_audit_enabled` (`APP_ANSWER_AUDIT_ENABLED`, default on) and
  `answer_audit_path` (`APP_ANSWER_AUDIT_PATH`, default `<ingestion_root>/answers-audit.sqlite`)
  drive the document-catalog composition root, and `AnswerService(audit=…)` stays optional so the
  offline gate and the script runners write nothing. `enterprise-pdf-rag audit --db … [--last N]
  [--fingerprint X] [--question-like …] [--show ID]` reads it back without starting a service or
  calling a model. Unlike `ragspine`'s privacy-aware traces this file keeps the evidence text —
  that is the point of it — so it stays a local file under the ingestion root, never served or
  shipped.


- **A page's regions are bound to the column they stand over** (`enterprise_pdf_rag`,
  [ADR 0013 amendment 1](docs/enterprise-pdf-rag/adr/0013-page-metadata-and-prefilters.md)).
  Region metadata was page-level, so p.13 of the AIA release — three `VONB ($m)` charts side by
  side, AIA Thailand **514**, AIA Singapore **294**, AIA Malaysia **232** — gave all three charts
  the same four region values, no filter could tell them apart, and across four cold runs the two
  Thailand questions were answered eight times and not once correctly. The new pure module
  `processing/column_regions.py` binds a page's verified region spans to the chart column each one
  stands over: `bind_columns(regions, columns)` over `PageRegionSpan(text, bbox)` and
  `PageColumn(member_id, bbox)`, with `MIN_COLUMNS = 2`, `MAX_COLUMN_HEADING_WIDTH_SHARE = 0.5`
  (a heading as wide as the page is the page's banner — `ASEAN` — and stays page-wide) and
  `MIN_HEADING_OVERLAP_SHARE = 0.5`. It is **all-or-nothing**: unless every column receives a
  heading and every heading finds a column it returns `EMPTY` and the caller keeps the page-level
  values it always used, because a layout that cannot be read must cost nothing rather than guess.
  No model call, no I/O, nothing on disk rewritten — `MemberText.member_regions` is an in-memory
  mount-time projection, so **no snapshot id changes and nothing is re-indexed**, and a release
  published before this module existed binds its columns at mount. `MountedDocument` supplies the
  geometry best-effort from each CHART member's `member_anchor` and from the page's source-text
  sidecar (the rectangles of the spans a region value was copied from, `MetadataEvidence.span_ids`),
  exactly as `member_anchor` already worked: geometry read for a refinement must never fail a mount
  the evidence itself supports. `member_matches` now filters on `member_regions or regions` while
  `region_vocabulary` still reports the whole page-level vocabulary, so nothing shrinks what a
  question can be parsed against; and the bound heading joins that member's contextual index header,
  so BM25 scores `AIA Thailand` on the Thailand chart. The stored vectors are untouched, so the
  lexical channel reads one phrase the embedding never saw — deliberate, and exactly why no
  published release needs re-indexing. A filter alone was not enough: `ContextBlock` also carries
  `regions`, and `SYSTEM_RULES` rule 8 says a question naming a region is answered from the block
  whose `regions=` names it and no other, in whatever language either is written, and otherwise
  abstains. On the real release p.13 binds its three charts to `AIA Thailand` / `AIA Singapore` /
  `AIA Malaysia` and p.12 binds its two to `Domestic` and `Chinese Mainland Visitor (CMV)`;
  **every other multi-chart page returns `EMPTY`** and keeps page-level regions, which is the safe
  path working as intended. `k01-region-thailand-en` and `k02-region-thailand-zh` both answer
  `514` `$m` from `points.point-1h26.value` on `a05e27202ea4…`, p.13, and move from
  `case_class: abstain` / `known_gap: true` to `case_class: positive` / `status: answered` with
  `forbidden_numbers: ["294", "232"]`, so a neighbouring column's number under Thailand's name
  fails the case by construction — **the gold set now carries zero known gaps**. The two are fixed
  for different reasons and the ADR says so: `k01` by the pre-filter, which now admits only the
  Thailand chart, `k02` only by the block header, because `泰国` matches nothing in the verified
  English vocabulary and the question is never translated. Still page-level: every non-chart member,
  and every member on a multi-chart page that does not read as columns. Cost: `member_texts()`
  0.26s → 0.43s, once per mount; mount time itself unchanged.

- **A gold requirement may name alternative anchors, and so may a filter expectation**
  (`enterprise_pdf_rag`, ADR 0011 follow-up). A frozen case asserted exactly one anchor per
  requirement, which silently asserted more than the evidence does: the pinned release states the
  record Operating ROE twice — the sentence on p.3 and the chart point on p.7 — and which one a
  run cites is not stable, so a correct answer failed. An element of `required_claims` and a
  `filters_expected` may now be written as `{"any_of": [...]}`: at least two alternatives, never
  nested, and never beside another key. Any single alternative satisfies the requirement, and
  when none does the report lists why each one failed rather than only the last.
  `adapters/nl_gold.py` discriminates the two written forms by shape
  (`Discriminator(_choice_form)` with `Tag`), so a malformed case reports against the form it was
  actually written in, and the single form parses and judges byte for byte as before —
  `schema_version` stays `nl-answers-gold-v1`. Three cases are re-pinned against it:
  `p01-roe-quote-en` and `p15-cache-repeat-en` accept either statement of the ROE, and
  `p06-donut-zh` enumerates the two period sets that were really observed — `{1H2026}` and
  `{1H2026, Y2026}` — because its applied pre-filters union what the question derives with what
  its translation derives (ADR 0018 amendment 1), and the translation is a real model call, so
  two cold runs derived different sets while giving the same answer from the same citations.
  Freezing either set alone would have asserted something untrue. The gold's `pinned` release is
  unchanged.

- **The wait for one model call is configurable** (`enterprise_pdf_rag`).
  `JsonCompletionClient` allows up to 180 seconds but `create_configured_app` never passed
  one, so every deployment was pinned to the 45-second default. The page context window
  (ADR 0017) makes a "summarise this section" question's prompt and generation long enough
  to cross it, and such a question then 503s on a default configuration —
  `Summarise the Growth Engines section for Hong Kong` did, at 48.3s. `AppSettings` gains
  `answer_timeout_seconds` (`APP_ANSWER_TIMEOUT_SECONDS`, default 45), validated against the
  same `(0, 180]` window the client enforces so a bad environment fails at startup rather
  than on the first question, and `scripts/enterprise_pdf_rag/webui_preview.py` passes it
  through to the API child like the other `APP_*` settings.

- **Retrieval picks its channels per question, and restates a foreign-language question in the
  index's language first**
  (`enterprise_pdf_rag`, [ADR 0018](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  Hybrid retrieval used to fuse the
  vector and BM25 rankings with RRF unconditionally; nobody had measured whether that helps. The
  2026-09-21 coverage probe (`data/validation/coverage-2026-09-21/`: 125 indexed facts from the
  AIA release, each asked two ways, pre-rerank) says it does not — BM25 alone recalls 74.4%
  within ten seats against fusion's 70.4%, and leads by more at every tighter cut (r@3 54.4% vs
  46.4%, MRR 0.482 vs 0.381), because RRF weights both rankings equally and the weaker one
  dilutes the stronger. `answers/query_mode.py` now classifies each question with no model and no
  I/O: at most five tokens **and** at most two content words, or a figure plus at most one
  content word, takes BM25 alone; a question the lexical channel cannot score takes the vector
  channel alone; everything else keeps fusion. Every threshold was swept offline against the
  probe's per-fact channel ranks, and the content-word budget (amendment 2) deliberately gives
  up the sweep's 74.4% ceiling for 73.6% — 92 facts of 125 — so that a short *phrase* like
  `Agency share of VONB 1H26` is no longer routed as though it were a label. 40% of the probe's
  queries (100 of 250) are rerouted and the rest behave byte-for-byte as before.
  `HybridSearch.search` gained a `mode` argument and
  returns a `SearchOutcome`; a single-channel mode is expressed as a fusion with one empty
  ranking, so scores stay comparable, and `bm25_only` skips the vector channel entirely — one
  embedding call fewer per request. `adapters/query_translation.py` restates a question written
  outside the index's language through one bounded, cached `complete_text_json` call (task salt
  `query-translation-v1`, strict `{english_query, source_language}` schema, rules that forbid
  answering and require figures and proper names to survive verbatim), triggered when the
  question's *content words* — function words and figures removed — score nothing lexically, so a
  Chinese question naming `1H26` is no longer mistaken for a scoreable one. The translation
  reaches the **lexical** channel only — BM25 and the channel classifier score it, while the
  vector channel and the rerank judge read the question as asked (amendment 3) — and the period /
  region pre-filters union what the question derives with what the translation derives
  (amendment 1). The prompt and the prose-number gate keep the original question, `SYSTEM_RULES`
  now asks for an answer in the question's language with claim text still copied verbatim from
  the evidence, and a translation
  that cannot be had is not an error — the question falls back to the vector channel alone.
  `AnswerRequest` gained `fusion_mode` and `translate_query`; `AnswerResult` and the
  `rag-chat-v1` `AnswerEnvelope` gained `fusion_mode` and `query_translation` as optional
  fields, and the checked-in schema was regenerated with nothing removed.

- **The model cache keeps the request it sent, not just the answer it got**
  (`enterprise_pdf_rag`). `JsonCompletionClient` recorded a fingerprint and a byte count for
  every call, so a cached answer could never be read back against the prompt that produced it.
  It now writes `model-cache/contexts/<request_fingerprint>.json` beside `requests/` and
  `responses/`: an envelope of `request_fingerprint` / `created_at` (UTC) / `endpoint_path` /
  `contract` / `task` around `payload`, the verbatim JSON body sent to the provider — system
  rules, every message (evidence blocks, page context, the question), the response schema and
  the token budget. A vision call replaces the inline `image_url` with
  `{"omitted": true, "sha256", "bytes"}` and keeps every other field untouched. The body is
  written before the call and back-filled when a cached answer is replayed without one; the
  first write for a fingerprint wins, and a write that fails (or disagrees) only leaves a code
  in that record's `diagnostics.context_warning` — it never fails the call. Successful records
  carry `diagnostics.context_path` back to the file. These files quote the source verbatim, so
  they stay local.

- **A frozen gold set for natural-language answers, with two runners that share one judge**
  (`enterprise_pdf_rag`, ADR 0011 follow-up). Until now the only frozen sets were the two typed
  ChartQA golds; the whole answer chain was re-measured by hand every round.
  `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json` freezes 25 cases
  against one pinned release (15 positive, 7 abstaining, 3 adversarial) and is registered in that
  folder's `manifest.json` (`aia-gold-registry-v1`). It freezes only what is stable across runs —
  `page_index`, `field_path`, `quote`, the claim's `value` / `unit` and the envelope's
  `filters_applied` / `filters_relaxed` / `cache_hit` — and never a `claim_id`, a `member_id`, a
  snapshot id or the prose wording. Where even that is not single-valued, a requirement or a
  `filters_expected` may name a set of alternatives (`any_of`) instead of one of them.
  `adapters/nl_gold.py` holds the strict schema (it self-checks
  on load: unique ids, a positive case must freeze or explicitly declare its claim, a known gap
  must say what the gap is) and `judge()`, the single pass/fail rule both runners use.
  `tests/enterprise_pdf_rag/answers/test_nl_gold.py` replays every case offline against the real
  pinned evidence through the production mount, with a declared vector channel and scripted model
  output, so the gold's anchors, seat selection, field-level verification, the prose numeric gate
  and the abstention policy are guarded by the default gate; it skips as a group when the release
  is absent or no longer the pinned one. `scripts/enterprise_pdf_rag/nl_gold_eval.py` runs the
  same cases against a live `document-catalog` service and writes a Markdown table, a JSON report
  and every raw response, exiting 1 on any failure that is not a declared known gap. Cases cover
  verbatim text quotes, chart values, diagram nodes, English / Chinese / keyword / title-only
  phrasings, opt-in rerank, derived and explicit pre-filters, a relaxed impossible filter, the
  completion cache, five abstentions and three probes that script an illegal model output.

- **Every hit is read beside the rest of its page, which is never citable**
  (`enterprise_pdf_rag`, [ADR 0017](docs/enterprise-pdf-rag/adr/0017-page-context-window.md)).
  Retrieval scores one member at a time, so a hit reached the
  prompt stripped of the page that explains it: a chart with no caption, a heading with no body, a
  bullet with no section. A page-level parent window now widens the generation context the way
  ragspine already does for narrative chunks (`src/ragspine/retrieval/link/narrative_link.py`: the
  window goes into a separate `prompt_text`, the citation stays pinned to the fine child).
  `processing/context_builder.py` gains `PageContextBlock` — one page's remaining members, each
  folded to a single line of its index-text body, in the reading order a proved diagram already
  uses (`READING_ROW_QUANTUM`, then left to right), under a `[page_context page_index=N]` head that
  prints the ADR 0013 page title and section once. `answers/page_window.py` (new, stdlib only)
  places one block per page after that page's first hit and leaves out members that already have
  their own block; `answers/ports.MemberText` gains `header` and `bbox` (the description's source
  anchor, or a chart's qualification receipt) plus a `body` property to feed it. The block
  deliberately prints **no field path and no member id**, so nothing in it can be the target of a
  claim: a model that names one anyway lands in the pre-existing `MODEL_OUTPUT_INVALID` /
  "unknown member" rejection, which no new code was written for and a test now pins. The prose
  numeric gate is widened to match — a figure the page context printed may be repeated without
  abstaining the whole answer — but that widens what the prose may *repeat*, never what it may
  *cite*, and only the members' own text is admitted, never the block rendering, so a head's
  `page_index=` cannot ground a number; the three adversarial gold cases still abstain unchanged.
  `budget_blocks` became generic over `PromptBlock` and gives page context up first, whole blocks
  from the last page backward, so a hit's own evidence is never surrendered to its neighbours'
  context; within a page, `page_window_budget_chars` (6000, against a total of 18000) drops whole
  members from the end and the block says `[truncated]`. Switchable at every level
  (`AnswerSettings.page_window`, `AnswerRequest.page_window`, `rag-chat-v1`'s `page_window`), and
  `AnswerResult.page_windows` / the envelope report every block that reached the prompt. No policy
  string moves, no snapshot id changes and no index is rebuilt — a release published earlier gains
  the capability as it stands, and the contract gains only optional fields.

### Performance

- **A mounted release is verified once, then watched for drift** (`enterprise_pdf_rag`).
  Mounting a document verified its whole pinned release — every content-addressed asset
  digest, the source it was cut from, every member's evidence — and then verified all of it
  again, in full, on every single request: `MountedDocument.manifest()` re-read and re-checked
  some 5000 assets, and each `resolve()` re-parsed the whole vector index behind it. On the
  pinned AIA release that was nearly all of an answer's latency with no model call in sight:
  **8.90s for one cache-hit question, of which 6.06s was `resolve` and 4.37s `manifest`**.
  The release is immutable and content-addressed, so after the mount that work proves exactly
  one thing — that nothing drifted. A request now re-reads the one file that names the whole
  release, the pinned manifest object whose digest *is* the processing id, and refuses any
  rewrite of it as before; size and mtime only skip re-hashing a file nothing has touched, a
  file that moved at all is re-hashed, and a mismatch falls through to the original full
  verification, which refuses. Beside that, a retrieval publication's plan and index are
  parsed once per process (keyed by their two content addresses), each member's evidence is
  hydrated once per mount (its first read still runs the full proof), and the vector ranking
  derives the snapshot's content address once instead of once per member — that last one was
  210 x 210 `asdict` calls, 0.9s, per query. `APP_VERIFY_EVERY_REQUEST=1`
  (`Settings.verify_every_request`) restores the old per-request verification for an audit.
  Measured in-process on the AIA release: **one question 8.90s -> 0.69s**, ten frozen gold
  questions **mean 9.00s -> 0.81s**, with every case's status unchanged. Mount time is
  unchanged, and the chart / displayed-bar requalification seams still rebuild the native and
  cropped SVG, the raw branches and the proof on every call.

### Changed

- **One release, one sampling: every completion request pins `temperature`, and a `seed` where one
  is configured** (`enterprise_pdf_rag`,
  [ADR 0018 amendment 4](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  `adapters/json_completion.py` sent no sampling at all, so a call ran on whatever the provider's
  defaults happened to be. Both request bodies — the vision `complete_json` and the text
  `complete_text_json` — now carry `DETERMINISTIC_TEMPERATURE = 0.0`, and a `seed` when
  `JsonCompletionClient(seed=…)` was given one: `Settings.answer_seed` (`APP_ANSWER_SEED`, default
  `0`), passed by the app factory to the one document-catalog client. The configured provider takes
  every shape of it — a direct probe of `gpt-5.6-luna` returned HTTP 200 for no sampling, for
  `temperature=0`, and for `temperature=0` with `seed=0` — so the `top_p` fallback this follow-up
  contemplated was neither needed nor written. **It invalidates every existing completion cache
  entry**, because the sampling sits inside the request body and the request body is what the
  fingerprint digests; that is the deliberate price of one release having one sampling, and it
  needs no re-run to inspect, since the `contexts/<fingerprint>.json` envelope is the wire body as
  sent and now records `"temperature": 0.0, "seed": 0`. **What it does not buy is a repeatable
  answer.** Two cold runs over the pinned AIA release, each against its own empty ingestion
  directory, sent byte-identical request fingerprints for **21 of the 22 model calls** and still
  worded **9 of the 22 answers** differently. The clearest case is the translation call, whose
  fingerprint `2b1a2d74…` was identical to the byte and which returned
  `Distribution channel proportion in 2026 first half` in one run and
  `2026 first half distribution channel proportion` in the other; the one differing fingerprint is
  a consequence of that — a different restatement is a different lexical query, a different BM25
  order and a different member set for `p06-donut-zh`, whose verdict did not change. The engine is
  deterministic — same question, same filters, same seats, same prompt — and this provider does not
  honour greedy decoding. The immutable completion cache remains the only real repeatability
  guarantee (`p15-cache-repeat-en`, `llm_live_calls=0`, every run).

- **A member is cited by a short alias, not by sixty-four hexadecimal characters**
  (`enterprise_pdf_rag`,
  [ADR 0011](docs/enterprise-pdf-rag/adr/0011-document-catalog-and-verified-answer-chain.md)
  follow-up). A claim names its evidence by the member's content address, and the prompt asked the
  model to transcribe all 64 hex characters of it. On a real run it copied **57** of them and
  `p05-donut-title-only-en` was lost whole to `model_output_invalid: unknown member` — a correct
  answer thrown away over a transcription. The prompt now mints `m1 … mN` over the blocks that
  really reach it: `ContextBlock.prompt_text` takes an optional `alias` and prints
  `[m3 | member <64hex>] kind=chart page_index=12 …`, `answers/prompt.member_aliases` numbers the
  citable blocks in printed order (a `page_context` block gets none, since rule 6 forbids citing
  it), and `resolve_member_aliases` rewrites a claim's alias back to the real id before anything is
  verified. A full id is still accepted "only when every one of its characters is copied", and a
  string nobody minted is left exactly as written so the verifier still rejects it as
  `unknown member`: nothing is guessed or repaired. Called without an alias `prompt_text` is
  byte-for-byte what it always was, so the listwise rerank judge is untouched. Aliases are minted
  after `budget_blocks`, from the blocks that really reach the prompt, and resolved before
  `verify_claims`, so the verifier, `ClaimCitation.member_id`, `AnswerResult.member_ids` and the
  HTTP contract all keep naming members by their real 64-hex id — the alias never leaves the one
  call. Cost: the prompt text changed, so this invalidates the completion cache as well.

- **A short question must be a short *label* to take BM25 alone**
  (`enterprise_pdf_rag`, [ADR 0018 amendment 2](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  `classify_query` sent any question of at most `MAX_BM25_ONLY_TOKENS` (5) tokens to the lexical
  channel alone. A token count is only a proxy for the shape that rule was measured on — every
  one of the 125 probe queries was a short label plus a period, one or two content words — and
  it is a leaky proxy: `Agency share of VONB 1H26` is five tokens but **three** content words, a
  phrase rather than a label, and BM25 alone drops the chart it needs from fusion's seat 7 to
  seat 12. The short clause now spends two budgets at once — at most five tokens **and** at most
  `MAX_BM25_ONLY_SHORT_CONTENT_WORDS` (2) content words — which is what every probe query
  actually was; the separate figure clause (a number plus at most `MAX_BM25_ONLY_CONTENT_WORDS`
  (1) content word) is untouched. The budget was swept over the same probe and its cost is
  stated rather than hidden: **recall@10 74.4% (93/125) -> 73.6% (92/125)**, recall@20 78.4% ->
  79.2%, MRR 0.476 -> 0.453, and 100 queries routed to BM25 against 150 to fusion where it was
  118 / 132. The single fact lost is `p09-a424f3446bd4` (`Strong Underlying Growth Drivers 1H26`
  — five tokens, four content words, exactly the shape this change reroutes, which BM25 simply
  happened to rank first). A budget of 4 holds 74.4% but leaves `Agency share of VONB 1H26` on
  BM25 and so fixes nothing: this is one probe fact traded for three real gold cases, on a probe
  whose corpus contains no question of that shape and therefore cannot measure it.

- **A translation is what the lexical channel scores, and only that**
  (`enterprise_pdf_rag`, [ADR 0018 amendment 3](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  A restated question replaced the query on **both** retrieval channels, which ADR 0018 recorded
  as deliberate but unmeasured. It is measured now, and it was wrong for the vector channel:
  against the pinned AIA release on Qwen3-Embedding-4B the Chinese question ranks the diagram it
  needs at seat **12**, and its own English restatement ranks it at seat **32**. The gap is not
  language but wording — the restatement writes `agents'` where the index prints `Agency` — and
  a multilingual embedding model reads the asker's own phrasing at least as well as someone
  else's paraphrase of it, while a token-matching channel cannot score that phrasing at all.
  `HybridSearch.search` gained `lexical_query: str | None`, which defaults to `None` and then
  scores one string on both channels byte for byte as before; `_QueryPlan` carries it. BM25 and
  `classify_query` now read the restatement, the vector channel and the rerank judge read the
  question as asked, and the period / region pre-filters still union what the question derives
  with what its translation derives (amendment 1), unchanged. One boundary is worth stating
  plainly: the restatement is a real model call, so a translated question's *retrieval input* is
  itself model output. Across the three of four real cold runs that shared one build, 20 of the
  22 gold cases returned byte-identical `member_ids`, and the two exceptions are exactly the two
  Chinese questions that trigger a translation (`p06-donut-zh`, `p11-diagram-zh`). Retrieval
  itself has no randomness; whatever non-determinism a translated question shows, it inherits
  from that one call — which is also why `p06`'s frozen `filters_applied` has to enumerate both
  period sets it really derives.

- **A chart point is retrievable when every one of its strings is printed in the figure**
  (`enterprise_pdf_rag`, [ADR 0016](docs/enterprise-pdf-rag/adr/0016-verbatim-chart-points.md)).
  `figure-source-labels-only-v1` could only compare a label against **one whole** source span,
  rejected any claim carrying a value or even containing a digit, and then blanked the chart
  (`axes=()`, `points=()`, `title=None`). A coverage measurement over the pinned AIA release
  (`data/validation/coverage-2026-09-21/`) put the cost at **2.6 % index coverage for chart
  facts — 4 of 156**: 20 of 29 chart objects were rejected whole, every one with the same
  `no_exact_source_labels` diagnostic, and the 9 that qualified lost their points anyway.
  Comparing all 281 chart IR fields verbatim against the figure's own spans showed only
  **3** were genuinely absent from the page; the rest needed a value and its unit read as one
  printed run (`33` + `%` is the single span `33%`), a label that wraps over two lines, a
  trailing parenthetical dropped, or the unit that is printed only in an axis title.
  A new scope `source-labels-and-verbatim-points-v1` keeps a point when its category **and**
  its value (with unit) each print verbatim inside the figure region, and drops the point
  otherwise — fail-closed per point, never per figure. `figures/source_label_match.py` is the
  rule (the cited occurrences in page reading order, one to three wide, each geometrically
  adjacent to the next, whitespace-folded concatenation **equal** to the label, case
  preserved, narrowest window wins); it is the chart sibling of the ADR 0013 page-metadata
  evidence window. The surviving points reach the ADR 0012 index projection and are citable
  through `points.<id>.value`. `figure-source-labels-only-v1` is frozen byte-for-byte and every
  gate accepts both, so snapshots published under it keep mounting and replaying.
  **What the new scope does not prove:** the category-to-value *association* is still the
  model's assertion. ADR 0008's `explicit-distribution-shares` — native sector geometry plus
  complete source-paint accounting — keeps its own name, its own receipt type and its own
  members, so a receipt always says which of the two guarantees a number carries.
  `adapters/visual_requalification.py` gained the matching Chart branch, so a published
  snapshot re-projects from its own stored branches with no model and no network.

### Fixed

- **A chart claim's unit is read before its number, and a unit the figure never printed is a
  refusal of its own** (`enterprise_pdf_rag`,
  [ADR 0016](docs/enterprise-pdf-rag/adr/0016-verbatim-chart-points.md)).
  `SYSTEM_RULES` asks a chart claim for "the displayed value with its unit", which was written
  for a figure that prints its own `%`. A `$m` figure prints the bare `294` under a `VONB ($m)`
  caption, so the verbatim source display is `294` while the model dutifully writes `294$m` —
  the prompt and the verifier contradicting each other, and every `$m` chart value on the pinned
  AIA release thrown out as `CLAIM_NOT_IN_EVIDENCE`. `_split_unit` now splits a claimed display
  into its number and whatever was printed around it (`294$m`, `294 $m`, `$294m`, `$294 m`,
  `8.2%`, `1,168 $m`, `-294 $m`, and the accounting `(294)`, whose brackets are how a figure
  prints a sign and so stay with the number); the number is compared verbatim against the source
  display and the unit verbatim against that point's own `unit.text`. **Nothing is normalised** —
  `294.0` still does not equal `294`, `1,168` keeps its separator — and a claim carrying no unit
  is checked exactly as before, so everything that used to pass still passes. It also closes a
  real hole in the other direction: `294%` against a `$m` point used to be **accepted**, because
  the verbatim comparison failed and the numeric fallback read `Decimal("294%")` as 294, so a
  claim contradicting the figure's own unit verified. A unit the point does not print — or any
  unit at all where the point prints none — is now a rejection in its own right, said as itself
  through the existing `AbstainReason.UNIT_MISMATCH`. That member was declared with the answer
  chain and never raised by anything; the typed ChartQA services refuse on their own
  `RefusalReason` / `DisplayedRefusalReason`, which only spell the same name. The enum is
  unchanged and nothing leaves the contract; what is new is that the claim re-read is its first
  caller, so `unit_mismatch` becomes reachable for the first time as a `rag-chat-v1`
  `abstain_reason`, `decide` reporting the first rejection's reason.
  `SYSTEM_RULES` is deliberately unchanged: rewording it would invalidate every
  cached completion and force the whole real-model gold set to be re-run.

- **A visual object's guaranteed seat is decided by its own channel ranks, not only by where
  fusion put it** (`enterprise_pdf_rag`,
  [ADR 0012](docs/enterprise-pdf-rag/adr/0012-chart-index-text-and-retrieval-seats.md),
  [ADR 0015](docs/enterprise-pdf-rag/adr/0015-diagram-and-formula-retrievable.md)).
  `select_context` promoted a missing visual kind only out of `ranked[top_k:2 * top_k]`, the next
  k *fused* positions. Reciprocal rank fusion, though, sorts a hit only one channel scored below
  every hit both channels contributed to — which is precisely the object the guaranteed seat
  exists for. The pinned release's only Diagram is one such object: the Chinese question ranks it
  at vector seat 12, while its English restatement shares no content word with that member
  (`agents` is not `agency`, and neither `technology` nor `investment` appears in it), leaving it
  at lexical seat 59 — past the channel limit of 50, so it contributes nothing to the fusion at
  all. With one channel's 1/(60+12) against seat 20's 0.01998 it fused at 30, outside the window,
  and the answer could read the diagram in its page context but had no citable block for it, so
  it correctly declined. The window is now the next k fused positions **or** any hit either
  channel ranked inside `2 * top_k` on its own ranking (`_within_a_channel`), and the call site
  asks for `2 * channel_limit` hits — the exact upper bound on the fused set — so
  `select_context` is handed the whole fused order instead of a prefix of it. The rule itself is
  unchanged: it fires only for a kind with no citable block in the head, seats at most one member
  per kind, gives up the last non-visual seat backward, and takes the first qualifying candidate
  in fused order. This supersedes the plain `2 * top_k` fused window recorded under 0.15.0.

- **A chart point whose id carries a decimal can be cited again** (`enterprise_pdf_rag`).
  A point id is derived from what the figure prints, so a value inside the label puts a
  decimal point in the id: the pinned AIA release prints
  `points.point-1h26-roe-17.5.value` in the prompt as a citable path. The verifier's path
  parser stopped the id at the first dot, so that claim was thrown out as
  `MODEL_OUTPUT_INVALID` before the point was ever looked up, and a faithful answer
  abstained (live `p01-roe-quote-en` / `p15-cache-repeat-en`, deterministically). Whatever
  a context block prints as citable must be readable back; the id may now hold dots, while
  `fullmatch` still anchors the path to its `.value` suffix.

- **A region filter matches the page's own qualification of the place, and never its
  exclusion** (`enterprise_pdf_rag`, [ADR 0018 amendment 1](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  Region pre-filters compared the filter value and the page's verified region value for
  equality. One document prints the same place several ways — `Thailand`, `AIA Thailand`,
  `Hong Kong`, `Hong Kong Special Administrative Region`, `Taiwan (China)` — so a question
  naming `Thailand` kept only the pages tagged with the bare word and dropped every page
  that actually carried the figure (frozen as the known gap `k01-region-thailand-en`). A
  filter value now matches when every one of its words appears as a whole word in the
  page's value, which widens `Thailand` from 29 to 51 candidate members on the pinned AIA
  release. A page value that *excludes* the place — `ex-Thailand`, `Asia ex-Japan`,
  `non-Hong Kong`, `Group excluding Thailand` — is the opposite claim, not a narrower one,
  and never matches: all 18 `ex-Thailand` members stay out.
- **The period and region pre-filters are derived from the translation too**
  (`enterprise_pdf_rag`, [ADR 0018 amendment 1](docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)).
  A question written outside the index's language cannot name a value of the document's
  verified English vocabulary, so no region filter was derived at all. When a translation
  was produced, `derive_filters` now runs over it as well and the two results are unioned
  (the question's own values first) before the candidates are recomputed. The pipeline
  order is unchanged, an explicitly supplied `filters` is never widened, and the prompt,
  the prose gate and claim verification still see only the question the user asked.

- **A number the question itself printed is grounded by its form, not by its spelling**
  (`enterprise_pdf_rag`). The prose gate admits a figure the user's own question already
  carries — a restated year or period is not a new figure — but it compared the captured
  token strings, and `_NUMBER_RE` reads thousands separators. The comma of "In 2024, VONB
  grew" was therefore captured into the token, so `2024,` never equalled the question's
  `2024` and a faithful, fully cited answer abstained (live `p02-year-filter-2024-en`,
  twice, with `numbers outside verified claims: 2024,`). A question's figures are now
  matched by the form they were written in — magnitude plus whether a percent sign was
  attached — so surrounding punctuation and thousands separators no longer count, while a
  bare `11` in the question still cannot ground `11%` in the prose.

## [0.15.0] - 2026-09-21

### Added

- **A ruled table's grid is proved from the page's own rulings** (`enterprise_pdf_rag`,
  ADR 0014): `TableIR` / `TableCell.verification` were pinned `PENDING` by construction, so a
  cell citation could quote the cell's text but never its row or column. They are now `VERIFIED`
  exactly when a proof exists — every row and column boundary sits on a real axis-aligned ruling
  from `page.get_drawings()` within 0.5pt, every cell edge is continuously ruled (collinear
  pieces stitched), and every merge is proved by the absence of a rule inside the merged cell.
  `processing/geometry.py` gains the ruling vocabulary, `processing/table_grid_proof.py` is the
  pure rule, and `adapters/pdfspine_tables.py` produces the observations (solid strokes, thin
  filled rectangles and stroked-rectangle edges; dashed, curved and over-thick paths are not
  rulings). Header rows / columns are graded: a thick interior rule or a filled band is `proved`
  and citable, a bold face or "first row" is a `heuristic` that never is. The stage receipt binds
  `grid_scope` + `ruling_digest`, and every `resolve` re-derives the whole proof from the pinned
  source PDF. An answer may then cite a cell's `row`, `col` and `header` — only on a verified
  grid, with the header matched verbatim apart from whitespace. Unruled, snapped and
  double-ruled tables stay `PENDING`; they remain retrievable and citable by cell text, and every
  snapshot published earlier still parses, mounts and resolves unchanged.
- **Diagrams and formulas become retrievable once their structure is proved from the source**
  (`enterprise_pdf_rag`, ADR 0015): `DIAGRAM` and `FORMULA` objects used to stop at a
  "no independent verifier" diagnostic (ADR 0006) and never reached the index, a context block or
  a citation. A **model-free, replayable** proof now runs beside the two model branches over the
  same pinned crop and, when it holds, writes `qualified_ir` / `qualified_description` /
  `qualification`. For a diagram (`adapters/diagram_geometry.py` + `diagram_qualification.py`,
  `processing/diagram_models.py` + `diagram_description.py`): every node label must equal its cited
  span verbatim, every node bbox must match a real painted frame within 2pt, every edge needs a
  connector leaving the source node plus a filled arrowhead whose derived tip lands in the target,
  and every span inside the object must be cited — otherwise the whole object fails closed with a
  verbatim diagnostic. A nodes-only diagram is admitted and prints no edge at all. For a formula
  (`processing/formula_models.py` + `formula_rules.py`, `adapters/pdfspine_formula.py` +
  `formula_qualification.py`): tokens quote span substrings under a tiling closure rule with
  per-character bboxes, superscripts and subscripts are proved from the PDF's own `Ts` or marked
  `derived`, fraction bars and radicals quote real `get_cdrawings()` paths, and every path inside
  the object must be explained. A fully proved formula is `VERIFIED` (`proof_level="full"`); one
  with a derived script is retrievable at `literal` level and stays `PENDING`. The description of
  both is a deterministic template, never a second model pass. Index-text policy rises to
  `source-transcription-and-scoped-chart-qualification-v5` (one new gate,
  `VISUAL_PROJECTION_POLICIES`, shared by both kinds); answers may cite `nodes.<id>.label`,
  `edges.<index>`, `formula.linear`, `formula.readable` and `tokens.<index>`, all compared
  whitespace-folded with case kept (`_literal` was renamed `_exact` and now serves every such
  check). `rag-chat-v1` gains `diagram` / `formula` block kinds and `diagram_node` / `diagram_edge`
  / `formula` claim kinds; `processing_export` gains `diagram_structure_qualified` and
  `formula_tokens_qualified` coverage columns. `scripts/enterprise_pdf_rag/requalify_visual_objects.py`
  re-proves an already-published snapshot's diagrams from the branches it already stores (no model,
  no pointer moved, `--dry-run` writes nothing) so a release can gain the capability without
  re-running `semantics`, and `scripts/enterprise_pdf_rag/formula_smoke.py` runs the formula proof
  read-only over a saved processing id. Snapshots published earlier still parse, mount and resolve
  unchanged.

### Fixed

- **A proved diagram or formula gets the same guaranteed prompt seat a chart has**
  (`enterprise_pdf_rag`, ADR 0012 generalised for ADR 0015): the AIA p6 three-stage pathway was
  qualified and indexed but never entered the ten prompt seats for "What are the three stages of
  the agency technology investment?", so the model answered from p5 prose instead.
  `adapters/answer_service.select_context` now keeps one seat per citable visual kind — a chart
  with an explicit value, a diagram with a labelled node, a formula with a linear form — for the
  first such member found within `2 * top_k` but outside `top_k`; seats are given up from the
  last one backward and never from a seat already holding a citable visual object, pending or
  label-only objects are never promoted, and only members of a still-missing kind in the window
  are resolved. The diagram index projection was confirmed to carry every node label.
- **A numbered list no longer trips the prose number gate** (`answers/verify.prose_grounded`):
  `1.` / `2)` / `3、` / `(4)` / `第 5` / `Step 6` at the start of a line or a sentence are
  enumeration markers, not figures, so a list-shaped answer with verified claims is answered
  instead of abstaining on "numbers outside verified claims". A number inside an item's body
  (amount, percentage, year) is gated exactly as before, and a decimal that ends a sentence is
  never mistaken for a marker.
- **Strict response schemas are guarded offline** (`tests/enterprise_pdf_rag/adapters/
  test_strict_response_schemas.py`): the ADR 0015 validation hit a provider-side HTTP 400 on every
  chat because an optional `ModelClaim` field left `required` incomplete (`a0a0d18`), which no
  offline test could see. A parametrised guard now walks the exact schema `_response_schema`
  sends for all nine `response_model` classes used at `complete_json` / `complete_text_json`
  call sites and enforces the strict-mode rules (every property required, `additionalProperties:
  false`, no unsupported composition keywords, `$ref`s local to `$defs`).

## [0.14.0] - 2026-09-21

### Added

- **`enterprise_pdf_rag` ships in the same distribution** (ADR 0021): `pip install rag-spine`
  now provides both `import ragspine` and `import enterprise_pdf_rag` (the traceable
  financial-PDF evidence / QA backend: content-addressed immutable snapshots
  source → processing → retrieval, span/drawing-level evidence chains, source-qualified
  ChartQA), plus a second console script `enterprise-pdf-rag` next to `ragspine`. History was
  preserved via `git subtree`; `ragspine` may only be imported under its `adapters/`, the
  `figures/ documents/ processing/` packages stay pure stdlib (guarded by
  `check_architecture.py`), and its four structural gates (conformance / architecture / schema /
  drift) run as `scripts/ci.sh` step 9. Its `CLAUDE.md` / `AGENTS.md` contract and `resources/`
  ship in the wheel; PRD, ADR 0001–0011 and JSON schemas live under `docs/enterprise-pdf-rag/`.
- **Generic PDF ingestion entry** (enterprise_pdf_rag ADR 0010):
  `enterprise-pdf-rag ingest --pdf <any PDF> --pages all|1-3,5 --stage source|layout|semantics
  --max-live-calls N` turns any PDF into an immutable draft; the default `source` stage makes
  zero model calls, the full source is always retained and page selection only scopes
  downstream work. `qualify → index → publish` stay explicit, with no implicit model call and
  no implicit activation. Runs outside the checkout with `APP_ROOT_DIR` / `APP_DATA_DIR`.
- **Document catalog, mounted documents and verified answer chain** (enterprise_pdf_rag
  ADR 0011): `APP_EXECUTION_MODE=document-catalog` scans published documents under
  `APP_INGESTION_DIR`, mounts each one and re-verifies its pinned manifest before any model
  call (drift / corruption → 409, missing embedder → 503, never a mock fallback). The
  `document-catalog-v1` contract adds `GET /v1/documents`, `GET /v1/documents/{id}`,
  `.../manifest`, `POST .../search` and `POST .../context`; hybrid retrieval pairs the pinned
  cosine vector channel with BM25 over the same description text, reusing `ragspine`'s
  retrieval / rerank pieces. `POST /v1/chat/completions` grows from source-review-only (422 on
  financial questions) into evidence-chain natural-language answering, every claim checked
  against its evidence under the family's anti-fabrication / provenance invariants. `TABLE`
  figures are admitted to the catalog and chart geometry matching gained an explicit tolerance.
- **Open WebUI `document-catalog` profile** for `scripts/enterprise_pdf_rag/webui_preview.py`,
  and `ENTERPRISE_PREVIEW_STATE_DIR` to relocate the preview's logs / PID record so a second
  preview can run beside an already recorded one.
- **`TableStructureRecognizer` seam** (`extraction/tables/`): given an already-detected table
  region plus its text-layer words, produce a cell grid (rows / columns / spans). Motivated by a
  2026-08 measurement on FinTabNet.c (150 pages / 186 gold tables): pdfspine's `strategy="text"`
  table **detection** is already good (79.6% recall, 100% precision — all 148 detections hit a gold
  table), while the **grid reconstruction** is what collapses (GriTS_Top 0.233 even on correctly
  located tables). The seam therefore deliberately does *not* do table detection.
  Cell *text* is never produced by this seam — a digital PDF's text layer is exact, so callers pull
  content from it by cell coordinates rather than letting a model read characters
  (the structure/content split that 2026 SOTA work such as DELTA also adopts).
  Five-part shape matching the family's other seams: Protocol + offline deterministic default
  (`GridStructureRecognizer`, word-centroid clustering, zero third-party deps) +
  `make_table_structure_recognizer` factory + `RAGSPINE_TABLE_STRUCTURE` env selection +
  parameterized conformance. **Default `None` = off**, so the existing extraction path is
  byte-identical; returning `None` means "no opinion" and the caller keeps its own grid — the seam
  never fabricates an empty grid to look like it answered.
- **TATR vision backend** (`extraction/tables/adapters/tatr.py`, new `[tsr]` extra): wraps
  Microsoft's Table Transformer structure-recognition model. Only its TSR half is used — detection
  stays with pdfspine — which saves one model's inference and removes an error source. Pixel
  coordinates are converted back to PDF points and clamped into the caller's region, so its output
  coordinate system matches the deterministic default exactly. torch/transformers/pillow are
  lazy-imported behind the extra with a friendly error when missing. The chosen checkpoint's licence
  must be checked against ADR 0009's ≤Apache-2.0 gate before promoting it to a default path.

- **A conditional prompt seat for a citable chart** (`enterprise_pdf_rag`, ADR 0012): when no
  hit in the top-k is a chart block with an explicit value but one sits within the next k fused
  positions, it replaces the last seat. Pending or label-only charts never qualify and nothing
  outside the window is promoted.
- **`AnswerEnvelope.member_ranks`** in the `rag-chat-v1` response: one optional
  `MemberRankOut(member_id, fused_score, vector_rank, lexical_rank, vector_score, bm25_score)`
  per member that entered the prompt, so retrieval behaviour is readable from a response. The
  contract name is unchanged.

- **`enterprise_pdf_rag` page-level automatic metadata and pre-filters** (enterprise_pdf_rag
  ADR 0013): a `page_metadata` processing stage — one text-only model call per page returns
  title / section / page type / language / periods / regions, each kept only when it quotes the
  page's spans verbatim (dropped with a diagnostic otherwise); periods normalise
  deterministically (`1H26` / `2026年上半年` → `1H2026`, `FY24` → `FY2024`, `Q1 2025` →
  `Q1-2025`, bare year → `Y2026`); document metadata (cover title, report period, years,
  region vocabulary) is a zero-model fold recomputed on load. `enterprise-pdf-rag metadata`
  annotates a saved draft or release; `ingest --stage semantics` runs the stage too and
  `--stage metadata` runs it alone. Index text policy v4 prepends
  `display_title | page_title | section` above the ADR 0012 projection (descriptions and
  evidence unchanged; older snapshots keep scoring what they embedded). `rag-chat-v1` gains
  optional `filters` (`periods` / `regions`, derived from the question when omitted; cover
  and agenda pages never enter the candidates; starved filters are relaxed and reported as
  `filters_applied` / `filters_relaxed`), citations gain `page_title`, `/v1/models` and
  `/v1/documents` show the verified display title, and an unnamed document is routed by
  distinctive cover-title words and years across mounted documents.

### Fixed

- **Prose number gate no longer rejects restated years / periods** (enterprise_pdf_rag
  ISSUE-3, `answers/verify.py::prose_grounded`): "…in 1H 2026 was 17.5%" abstained as
  `claim_not_in_evidence` because `2026` was not inside a verified claim's text. A prose number
  is now grounded when it equals a verified claim's text number or value, appears verbatim in
  the user's question, or equals a number in the evidence text the verified claims cite (span
  quote, table cell text, chart period / category labels and source display). Any other number
  still abstains the whole answer; zero verified claims and the `decide` order are unchanged.
- **`scripts/enterprise_pdf_rag/webui_preview.py` no longer requires `lsof`**: a recorded PID's
  working directory is read from `/proc/<pid>/cwd` on Linux and from `lsof` only where `/proc` is
  absent (macOS); an unreadable directory never matches, so unrelated processes are still refused
  instead of crashing on a runner without `lsof`.

### Changed

- **`enterprise_pdf_rag` indexes charts by a projection of their qualified IR** (ADR 0012).
  A chart member used to embed its description, which is often its title alone, so a question
  naming the chart's categories or values had nothing to match. `processing/index_text.py` now
  projects a chart with at least one explicit point value into
  `<title> <period> <grammar> chart figure` plus `<category> <series> <value><unit>` per point;
  a pending, label-only or valueless chart keeps its description, and text / list / group / table
  members are unchanged. Description assets are untouched. The retrieval policy moves to
  `source-transcription-and-scoped-chart-qualification-v3` (bar publication to
  `source-transcription-donut-and-displayed-bar-v2`) and BM25 is gated on the same policy set, so
  both channels always score the string that was embedded — on old snapshots too. Policy strings
  stay informational: **existing releases keep mounting and answering, but only a re-run of
  `index` + `publish` gives them the projection.**
- **`enterprise_pdf_rag` retrieval defaults widen**: `AnswerRequest.top_k` 6 → 10 and
  `channel_limit` 20 → 50 (neither is exposed on the `rag-chat-v1` request, so the contract is
  unchanged). The opt-in reranker now judges evidence blocks — what the answer model would see —
  instead of a concatenation of index texts; it stays off by default because it must resolve
  every fused candidate.
- **Base dependencies**: `httpx>=0.27` moves into the base install — `enterprise_pdf_rag`'s HTTP
  layer imports it statically, so `enterprise-pdf-rag --help` failed on a plain
  `pip install rag-spine` without `[service]`. A new guard test
  (`tests/enterprise_pdf_rag/test_base_dependencies.py`) asserts that every third-party import
  of `enterprise_pdf_rag` is covered by `[project].dependencies`. Also in base for the sibling
  package: `pydantic-settings[yaml]`, `jinja2`, `fastapi` / `uvicorn`, `pdfspine`, `resvg-py`,
  `fonttools`; `corespine>=0.5.1`, `pdfspine>=0.11.0`, `pydantic>=2.12,<3`.
- **Toolchain**: ruff 0.16 formatting across the repo; `mypy --strict` covers `src/ragspine`, `src/enterprise_pdf_rag`,
  `tests/enterprise_pdf_rag` and `scripts/enterprise_pdf_rag`; `uv` 0.12.17.

## [0.13.0] - 2026-08-03

### Added

- **OpenAI Chat Completions compatibility** (`service/api/openai_public.py`): `POST /v1/chat/completions`
  (blocking + SSE streaming) and `GET /v1/models` clone the official OpenAI shape, so `openai` SDK
  clients, Open WebUI, LangChain, and any OpenAI-compatible provider slot can talk to RAGSpine
  unchanged. Provenance is preserved through a non-standard top-level `ragspine` extension field
  (`route` + `sources` + `request_id`); OpenAI clients ignore unknown fields, so lineage is never
  dropped to fit someone else's signature. Reuses the `/v1/ask` guard chain verbatim and keeps the
  guard-before-stream invariant (the generator replays an already-guarded answer, no provider/store
  access). Client-supplied `system` messages are deliberately ignored — the system prompt stays
  server-controlled.
- **LightRAG-shaped Python adapter** (`ragspine/compat/lightrag.py`): `LightRAG` + `QueryParam`
  clone HKUDS/LightRAG's public surface (`insert` / `ainsert` / `query` / `aquery` /
  `initialize_storages`) so existing LightRAG call sites migrate by changing one import. It is a
  thin signature translation over the `RAGSpine` facade — no retrieval logic is reimplemented.
  Because LightRAG's `query()` returns a bare string and would swallow lineage, an extra
  `query_with_sources()` returns the full `AgentResult`. Inserted raw text is landed as a
  content-addressed `.txt` under the workspace and ingested through the normal pipeline, so it
  gets real `doc_id` + locator provenance instead of becoming an unsourced dangling chunk.
  Semantic gaps (mode mapping, no Leiden hierarchy, ignored LightRAG-only kwargs) are documented
  rather than papered over.
- **Microsoft GraphRAG artifact interop** (`ragspine/compat/graphrag.py`, new `[graphrag-compat]`
  extra): `import_graphrag_artifacts()` loads `entities` / `relationships` / `text_units` parquet
  from a `graphrag index` output directory into any `GraphStore`, and `export_graphrag_artifacts()`
  writes a subgraph back out in that shape. GraphRAG exposes no Python API — its real contract is
  the parquet layout — so interop is done at the artifact layer. Imported records get lineage
  back-traced through `text_units` (never left empty) and are stamped
  `derived=model-derived` + `verified=unverified`; export goes through `GraphStore.subgraph`, so
  RESTRICTED nodes can never leak into files handed to an external tool. pandas/pyarrow are
  lazy-imported behind the extra, keeping the default install unchanged.

## [0.12.1] - 2026-07-30

### Changed

- Relaxed the Python requirement back to `>=3.12` (no upper bound); 0.12.0's 3.14-only floor
  is lifted. Restored the quoted self-referential annotations that 3.12/3.13 need (no PEP 649
  lazy evaluation there); toolchain (ruff/mypy) and CI matrices now target 3.12–3.14.

## [0.12.0] - 2026-07-21

### Added

- High-level `RAGSpine` workspace facade with unified dual-channel ingestion and guarded asking.
- `economy`, `balanced`, and `quality` retrieval presets with explicit typed overrides.
- Installed `ingest`, `doctor`, `config init/show`, and zero-Redis local `serve` CLI paths.
- Effective-configuration provenance and offline dependency, key, model, and filesystem diagnostics.
- Per-file ingestion channel, fact, chunk, review, skipped-page, warning, and remediation feedback.

### Changed

- **Breaking**: RAGSpine now requires Python 3.14 exclusively (`>=3.14,<3.15`). Python 3.11–3.13
  users stay on 0.11.0. Toolchain (ruff/mypy), CI matrices, and Docker images target 3.14.
- The package-root API now exposes the `RAGSpine` facade alongside the four original primitives.
- Installed users can complete ingestion, querying, and local visualization without repository scripts.

[Unreleased]: https://github.com/VoldemortGin/ragspine/compare/v0.16.1...HEAD
[0.16.1]: https://github.com/VoldemortGin/ragspine/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/VoldemortGin/ragspine/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/VoldemortGin/ragspine/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/VoldemortGin/ragspine/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/VoldemortGin/ragspine/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/VoldemortGin/ragspine/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/VoldemortGin/ragspine/compare/v0.11.0...v0.12.0
