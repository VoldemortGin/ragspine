---
covers: src/enterprise_pdf_rag/
verified-against: 9540dbd
---

# enterprise_pdf_rag — agent contract

Auto-loaded when working under `src/enterprise_pdf_rag/`. Keep terse; the long-form docs live
in `docs/enterprise-pdf-rag/`. This is a **sibling package** of `ragspine` in the same repo and
the same `pyproject.toml` — import name unchanged, not under `ragspine.*`
([ADR 0021](../../docs/adr/0021-merge-enterprise-pdf-rag-as-sibling-package.md)).

## Read first (in order)

1. [`AGENTS.md`](AGENTS.md) — project rules (scope, one TDD slice at a time, offline default).
2. [`docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md`](../../docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md) —
   the top “会话收尾状态” and “当前后续工作顺序” win over the historical snapshots kept below
   them.
3. [ADR 0009](../../docs/enterprise-pdf-rag/adr/0009-source-qualified-expense-ratio-bar-lookup.md),
   [ADR 0010](../../docs/enterprise-pdf-rag/adr/0010-generic-pdf-ingestion-entry.md),
   [ADR 0011](../../docs/enterprise-pdf-rag/adr/0011-document-catalog-and-verified-answer-chain.md),
   [ADR 0012](../../docs/enterprise-pdf-rag/adr/0012-chart-index-text-and-retrieval-seats.md),
   [ADR 0013](../../docs/enterprise-pdf-rag/adr/0013-page-metadata-and-prefilters.md),
   [ADR 0014](../../docs/enterprise-pdf-rag/adr/0014-ruled-table-grid-proof.md),
   [ADR 0015](../../docs/enterprise-pdf-rag/adr/0015-diagram-and-formula-retrievable.md),
   [ADR 0016](../../docs/enterprise-pdf-rag/adr/0016-verbatim-chart-points.md),
   [ADR 0017](../../docs/enterprise-pdf-rag/adr/0017-page-context-window.md),
   [ADR 0018](../../docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md),
   [ADR 0019](../../docs/enterprise-pdf-rag/adr/0019-document-tree-channel.md),
   [ADR 0025](../../docs/enterprise-pdf-rag/adr/0025-lite-ingest-mode.md)
   and [PRD v0.2](../../docs/enterprise-pdf-rag/PRD-v0.2.md) define scope; the full list is
   [`docs/enterprise-pdf-rag/adr/`](../../docs/enterprise-pdf-rag/adr/).
4. [`testing-and-ingestion.md`](../../docs/enterprise-pdf-rag/testing-and-ingestion.md) — what is
   actually testable today. Source review or an HTTP 200 is **not** a finished generic RAG.

## What lives here

Traceable financial-PDF RAG backend: content-addressed **immutable snapshots**
(source → processing → retrieval), an **evidence chain** from PDF span/drawing to answer, and
**source-qualified ChartQA**. Natural-language answering over that chain exists (ADR 0011):
hybrid retrieval → evidence blocks → one bounded model call → field-level claim verification,
served by the `document-catalog` mode. It is verified offline against authored PDFs; real-model
acceptance is recorded in the handoff, never assumed here.

```
documents/    aia.py only (the pending AIA sample identity); the pure document model moved to
              ragspine.extraction.evidence.document (ADR 0022)
processing/   context_builder.py (evidence blocks for the prompt, plus the uncitable page
              context block each hit is read beside — ADR 0017), index_text.py (contextual
              header + chart / diagram / formula projection both retrieval channels score),
              retrieval.py; the rest moved to ragspine.extraction.evidence.{page, metadata,
              objects}, and figures/ to ragspine.extraction.evidence.figures (ADR 0022)
answers/      pure answer chain — ports.py (MountedDocument, MemberText), models.py
              (MemberFilters, TranslatedQuery), prompt.py (strict model output schema),
              verify.py (claim re-read), page_window.py (one page context block per hit
              page, its members in the diagram reading order — ADR 0017),
              query_filters.py / member_filter.py (period / region pre-filters derived
              from the question, relaxed when they starve), query_mode.py (which
              retrieval channels a question uses — ADR 0018); stdlib + pydantic only
adapters/     every SDK and I/O: pdfspine (every source PDF opens through pdf_password.open_pdf,
              which authenticates a password-protected one with PDF_INGEST_PASSWORD or refuses
              it with a ValueError naming that setting), http/ (FastAPI app factory; documents.py + chat.py
              serve document-catalog mode), local models, stores, draft_publication.py
              (qualify / index / publish), page_metadata_extraction.py (page_metadata stage),
              document_catalog.py (scan / mount), hybrid_search.py (BM25 + RRF + opt-in
              rerank borrowed from ragspine; the channels a query uses are chosen, not
              fixed — ADR 0018), query_translation.py (restate a question written outside
              the index's language), document_tree_extraction.py (the document_tree
              ingestion stage: one routing note per branch, the structure saved even when the
              budget defers them) + tree_retrieval.py (one bounded call routing a question to
              a handful of pages — ADR 0019), answer_service.py (one synthesis call per answer),
              answer_audit.py (the local sqlite answer journal: one row per question, opened
              with the prompt as sent and closed with the verified result),
              diagram_geometry.py + diagram_qualification.py + diagram_publication.py and
              pdfspine_formula.py + formula_qualification.py (the ADR 0015 proofs, receipts
              and replays), visual_requalification.py (re-prove a saved snapshot's visual
              objects from its stored branches), chart QA v1/v2, nl_gold.py (the frozen
              natural-language gold set's schema and the judge both its runners share),
              nl_gold_runner.py (one case over an injected chat `post`: body, envelope,
              verdict), folder_pipeline.py (`run-folder`: every PDF under a folder through
              ingest → requalify → qualify → index → publish → tree, budgeted and
              resumable, then a question set answered in process; `folder` / questions / report
              default to NB_PDF_DIR / NB_QUESTIONS_PATH / NB_REPORT_DIR, an argument wins
              (`notebooks/run_folder.ipynb` ignores NB_REPORT_DIR: it pins reports to
              ROOT_DIR/data/reports/<question-set stem> and guards that every write dir is under
              ROOT_DIR/data and outside the read-only PDF dir; the guard is notebook-only, and it
              writes <report dir>/answers.csv = question, expected, answer from each `EvalCase`'s
              `question` / `expected` / `answer`; the answer prose never enters a trace or event;
              `max_questions=N` answers N questions (notebook `MAX_QUESTIONS = 10`; the CLI does not
              expose it) — `question_selection="first"` the first N, `"first_matched"` (not the
              notebook default, which is `"first"`) the first N whose `doc` names exactly one PDF of the folder, the rest listed
              as skipped);
              question_docs.py (ADR 0022: every `doc` resolved once, before any work, by alias →
              exact name → stem → sha prefix → NFKC/separator-normalized name; ambiguous or a near
              miss never matches, near misses are only `candidates`; the same resolution feeds the
              `only_question_docs` selection (others `skipped_not_referenced`, unread) and the
              answer routing; `doc_aliases`; `on_unmatched_docs="error"` stops before any write,
              `"skip"` answers those as `routing_failed`; `check_question_docs` is the read-only
              check the notebook's `question-docs` cell prints); per-PDF ingest ceiling
              `MAX_INGEST_LIVE_CALLS` = 10 000, or `max_live_calls_per_pdf="auto"` (notebook default):
              min(ceiling, selected pages × 4 + 50) from the page count the source stage reads,
              no extra PDF open, still bounded by the shared total; `IngestionSummary.pages_complete` /
              `pages_budget_deferred` / `pages_claim_blocked` (and the call-level `calls_claim_blocked` /
              `claims_taken_over`, ADR 0023) and `document_done.pages="X/Y"` show a
              partly ingested `published` document; `document_progress` reports pages / calls /
              cache hits, counts only;
              a failed document's `document_done` progress event carries failed_stage / error;
              `FolderPipelineResult.sampling_parameters_dropped`, a `report.md` line and a
              `sampling_parameters_dropped` event name the sampling parameters the LLM endpoint
              refused, ADR 0021),
              ingest_mode.py (ADR 0025: `run_folder_pipeline(ingest_mode="full"|"lite")`,
              default full = byte-identical; `IngestPlan` holds one field per switch — image /
              formula calls, chart description from IR, deterministic page metadata, default
              tree, review exports, `unverified_tables_as_rows` (ADR 0027: lite on, full off)
              and `layout` (`make_partitioner`: `"model"` in both presets,
              `"deterministic-text-pages"` only when asked — ADR 0028); `ingest_pdf` /
              `run_folder_pipeline` take `layout_policy` / `unverified_tables_as_rows` as
              one-switch overrides of the preset (`None` keeps it); `published_ingest_mode` reads a
              snapshot's mode back from its page-metadata producer; lite after full sends no call,
              full after lite only the skipped ones; `export_document_review` in pdf_ingestion.py
              writes review pages on demand),
              deterministic_partition.py + deterministic_partition_geometry.py (ADR 0028:
              `layout_policy="deterministic-text-pages"` partitions pages without figures /
              images straight from pdfspine blocks — zero layout calls, producer
              `page-layout-deterministic-v1:pdfspine/<version>`, every span owned, ruled
              tables only via `find_tables("lines")` + exact cell-span consistency — and
              falls back per page to the model layout with a machine-readable reason code
              (`IngestionSummary.pages_partitioned_deterministically` /
              `pages_partition_model_fallback` / `partition_fallback_reasons`); `"model"`
              keeps every page on the model layout, byte for byte),
              answer_llm.py (`make_answer_llm()`: the answer `JsonCompletionClient` built from
              settings, what `run_folder_pipeline` and `notebooks/run_folder.ipynb` call)
resources/    packaged prompts / static data
cli.py        enterprise-pdf-rag ingest|metadata|tree|qualify|index|publish|run-folder|serve|audit|chart-qa|demo|extract|llm-smoke
              + AIA-sample-only ingest-aia|process-aia-layout|process-aia-semantics|index-aia-processing
_moves.py     frozen legacy → canonical module map (ADR 0022: this package is dissolving into
              ragspine.<domain>.evidence; PENDING = the AIA lane, which leaves the wheel)
_shim.py      meta path finder, installed before the hook: a moved module's legacy name is the
              same module object as its canonical one, with a DeprecationWarning
```

Structure is enforced by `scripts/enterprise_pdf_rag/check_conformance.py` (src layout, beartype
hook, absolute imports, closed import whitelist outside `adapters/`), `check_architecture.py`
(pure `processing/` `answers/` and the `ragspine.<domain>.evidence` pure homes; only
`answers/` may import pydantic),
`check_schema.py` (`docs/enterprise-pdf-rag/schemas/*.json` ⇄ pydantic boundary models) and
`check_drift.py`. `ragspine` is imported only under `adapters/`.

## Run (always from the repo root)

- **Tests:** `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q` — the suite's own
  `conftest.py` enforces no network. It is also collected by the repo-wide run.
- **Gate:** `bash scripts/ci.sh` — step 5 runs this suite, step 9 the four checks above.
  The old `./ci.sh` no longer exists.
- **Config:** `config/enterprise-pdf-rag/settings.yaml`. **Runtime data:** `data/`
  (gitignored; APFS-cloned from the original repo — never clean or overwrite `data/output`,
  `data/validation`, `data/samples` or the `current-*` pointers).
- **document-catalog mode:** `APP_EXECUTION_MODE=document-catalog enterprise-pdf-rag serve`
  mounts every `ready` document under `APP_INGESTION_DIR` (default `data/ingestion`) plus the
  processing-store roots listed in `APP_LEGACY_DOCUMENT_ROOTS` (JSON list; the AIA release,
  default empty). `OPENAI_EMBEDDING_MODEL` (same gateway as the LLM; or `APP_EMBEDDING_*` for a separate loopback
  service) enables search, `OPENAI_*` (alias `APP_LLM_*`, read per field) enables chat, `APP_RERANK_*` enables
  opt-in rerank; a missing group is a 503 on its routes, never a mock. Model cache
  `<ingestion_root>/model-cache` — `requests/<fingerprint>.json` the record, `responses/`
  the bodies, `contexts/<fingerprint>.json` the **full request body as sent** (system rules,
  every message, schema, token budget, and the sampling — by default it records
  `"temperature": 0.0, "seed": 0`; inline images summarized), written before the call and
  back-filled on replay, first write wins. It quotes the evidence verbatim: local files, never
  shared. Live budget `APP_ANSWER_MAX_LIVE_CALLS` (200), per-call wait
  `APP_ANSWER_TIMEOUT_SECONDS` (45, capped at 180 — a page window makes a "summarise this
  section" prompt long enough to need more), sampling seed `APP_ANSWER_SEED` (0; a `seed` is
  sent only when one is configured), temperature `OPENAI_TEMPERATURE` (alias
  `APP_LLM_TEMPERATURE`; unset → `0.0`, a number in [0, 2], or `omit` to send none) — the
  sampling is inside the request body, so changing it misses every cached completion. An
  endpoint that refuses `temperature` / `seed` (HTTP 400 naming it in `error.param`) gets the
  call again without it, remembered per (endpoint, model) and on disk; a 400 record written
  before that body was read gets one re-probe ([ADR
  0021](../../docs/enterprise-pdf-rag/adr/0021-sampling-parameter-fallback.md)).
- **Answer journal:** every answer writes one row to `<ingestion_root>/answers-audit.sqlite` — the
  final `prompt_system` / `prompt_user` verbatim (opened *before* the call), the fused ranking with
  each channel's seat, then the raw model output, the verified / rejected claims and the status.
  `APP_ANSWER_AUDIT_ENABLED=false` writes nothing; `APP_ANSWER_AUDIT_PATH` moves the file. Read it
  back with `enterprise-pdf-rag audit --db <path> [--last N] [--fingerprint X] [--question-like …]
  [--show ID]` (no service, no model). It quotes the evidence verbatim: local file, never shared.
  Its `ranked` column (added later; older rows NULL) is the whole fused ranking with each member's
  page and BM25 / vector / tree seat. **Retrieval test bench** (`adapters/retrieval_testbench.py`,
  `audit --testbench --question-set <path> [--report …] [--format table|json|csv] [--write]`, or
  `run_retrieval_testbench(...)` in a notebook): one row per question — routing, pre-filters, each
  channel's seat for the expected page, in prompt or not, status, `content_hit` — and a diagnosis
  (`routing_failed` / `not_retrieved` / `retrieved_not_in_prompt` / `in_prompt_abstained` /
  `in_prompt_wrong` / `correct`, plus `not_in_prompt` / `unjudged` / `no_record` where the record
  cannot tell); questions link to their latest row by text. Read-only, no model; writes
  `testbench.csv` / `.json` only under `ROOT_DIR/data`, and logs nothing.
- **Deploy:** `deploy/enterprise-pdf-rag/open-webui/` — `backend.Dockerfile` is not re-verified
  since the merge (local `../corespine` uv source; see ADR 0021 follow-ups).
- **Benchmarks / gold:** `data/benchmarks/enterprise-pdf-rag/aia-2026-interim/` (its `manifest.json`
  registers each set). Besides the two typed ChartQA golds there is now a frozen
  **natural-language** gold set, `nl-answers-gold-v1.json`, pinned to one published release:
  schema + the single pass/fail rule in `adapters/nl_gold.py`, replayed offline by
  `tests/enterprise_pdf_rag/answers/test_nl_gold.py` and run against a live service by
  `scripts/enterprise_pdf_rag/nl_gold_eval.py` (**run that one before a release**; see
  `testing-and-ingestion.md` for `known_gap` semantics).

## Invariants (do not break)

- **Explicit mode, never silent mock** — `aia-source-review` / `document-catalog` /
  `offline-demo` / production is chosen explicitly; the default gate calls no model or network
  service.
- **Verified claims only** — every model claim is re-read from stored evidence field by field
  (verbatim quote, exact cell text, a chart value whose number equals the qualified `Decimal`
  or its exact source display and whose unit, when it states one, is the point's own, cited
  back to SVG elements); failed claims are dropped, and any number in the prose outside a
  verified claim abstains the
  whole answer (ADR 0011). One synthesis call per answer, plus at most one earlier
  translation call for a question written outside the index's language (bounded, cached,
  skipped when unavailable — ADR 0018); `llm_live_calls` counts both. Nothing is derived or
  retried, and a translation only ever reaches retrieval — the **lexical** channel (BM25 and
  the `classify_query` routing it feeds) and the period / region pre-filters, which union it
  with what the original question derived. The vector channel and the rerank judge keep the
  question as asked: both read it as language, so a restatement only trades the asker's
  wording for someone else's — measured on this corpus the original ranked the object it
  needed at seat 12 and its own translation ranked the same object at seat 32, a difference
  of wording rather than language (the translation wrote `agents'`, the index prints
  `Agency`) — ADR 0018 Amendment 3. The prompt and the prose gate keep the original
  question, and claims stay verbatim.
- **A chart claim's unit is read before its number** — a claimed chart value is split
  deterministically into the number as written and whatever unit was printed around it
  (`294$m`, `294 $m`, `$294m`, `8.2%`, `1,168 $m`, the accounting `(294)`). The number is then
  compared verbatim against that point's source display, the unit verbatim against that
  point's own `unit.text`, and **nothing is normalised numerically**: `294.0` is still not
  `294` and `1,168` keeps its separator. A claim stating no unit is judged on its number
  alone, exactly as before. A unit the point does not print is a rejection in its own right
  rather than a number that happened not to match, and so is a unit claimed against a point
  that prints none. The reason both carry, `AbstainReason.UNIT_MISMATCH`, was declared with
  the chain and until now nothing raised it; the claim re-read is its first caller, which is
  what makes `unit_mismatch` reachable as a `rag-chat-v1` `abstain_reason` at all, the first
  rejection's reason being the one the envelope reports. Both directions were wrong before:
  `prompt.SYSTEM_RULES` asks for the displayed value
  *with its unit* while a `$m` figure prints a bare `294` under a `VONB ($m)` caption, so a
  correct `294$m` was refused; and `294%` was *admitted*, because the failed display
  comparison fell through to a numeric check that read it as 294 and let a claim
  contradicting the figure's own unit pass.
- **Retrieval channels are chosen per question, and the choice is reported** (ADR 0018) —
  a short label-and-period question is answered from BM25 alone (measurably better than RRF
  fusion on this corpus: the channels themselves recall 74.4% within ten seats against 70.4%
  for fusion and 48.0% for the vector channel), a narrative question keeps fusion, and a
  question the lexical channel cannot score takes the vector channel alone. **Short spends two
  budgets at once** — at most `MAX_BM25_ONLY_TOKENS` (5) tokens *and* at most
  `MAX_BM25_ONLY_SHORT_CONTENT_WORDS` (2) content words — because a token count alone reads
  `Agency share of VONB 1H26` (five tokens, three content words) as a label and answers a
  phrase from BM25, which drops the chart it needs from seat 7 under fusion to seat 12
  (ADR 0018 Amendment 2). The separate figure clause still spends the tighter
  `MAX_BM25_ONLY_CONTENT_WORDS` (1), where the token count is already over. On the 125-fact
  probe the content-word budget costs one fact and buys one — recall@10 73.6% (92/125) against
  74.4% (93/125), recall@20 79.2% against 78.4%, r@3 52.8%, r@5 56.8%, MRR 0.453 against
  0.476, 100 questions routed to BM25 and 150 to fusion against 118 / 132 — an honest trade of
  one probe fact for three real gold cases, because every probe query is a short label plus a
  period (one or two content words) and the probe therefore cannot measure the question shape
  this budget exists for. `fusion_mode` and `query_translation` in `AnswerEnvelope` say which
  ran. A query embedder is a dependency of the requests that use it, exactly like the opt-in
  reranker: a BM25-only question is answered
  without one, a question needing the vector channel is still 503 with no substitute.
- **What gets embedded is the index text** of
  `processing/index_text.py`: the page's contextual header (`display_title | page_title |
  section`, ADR 0013) above the natural-language description for text / list / group / table
  members, and for a chart a deterministic projection of its already-qualified IR (title, period,
  grammar, per-point category / series / explicit value), falling back to the description when the
  chart has no citable value (ADR 0012); a proved diagram projects its node labels in reading order
  plus one `<from> -> <to>` per drawn edge, and a proved formula its readable and linear forms plus
  every token text (policy v5, ADR 0015). Both retrieval channels score that same string; the raw
  branch is never embedded; description assets are never rewritten.
- **Page context informs, it never cites** (ADR 0017) — beside each hit the prompt prints the
  rest of that hit's page, one line per member in reading order, with no field path and no
  member id; it is generation context only. A claim naming it is an `unknown member` and is
  dropped as `MODEL_OUTPUT_INVALID`; the answer abstains only if no verified claim survives.
  The prose numeric gate admits a figure the page context
  printed — that widens what the prose may repeat, never what it may cite, so such a figure
  carries no citation. When the prompt budget overruns, page context is given up first (whole
  blocks, last page backward); a hit's own evidence is never surrendered.
- **A tree node's summary routes, it never testifies** (ADR 0019) — a document's outline is
  folded deterministically from verbatim page metadata, but each branch's routing note is
  written by a model (`document-tree-summary-v1`), so it never leaves the routing call: it is
  never indexed, never reaches an answer prompt's evidence blocks, and can never be cited. It
  carries no member id and no field path, so a claim naming one is an `unknown member` dropped
  as `MODEL_OUTPUT_INVALID`, exactly as page context is. The router's whole output is a page
  set (at most `MAX_ROUTE_NODES` / `MAX_ROUTE_PAGES`, 6 each), and those pages join the fusion
  as a **third ranking, never a pre-filter** — a filter can only remove, so one bad route would
  hide the answer, while a bad ranking costs rank and not recall. That ranking **routes, it never
  displaces**: it is fused at its own `tree_rrf_k`, which `HybridSearch.__init__` refuses to run
  without `tree_rrf_k + 1 > rrf_k + channel_limit`, so a member **only** the tree reached sorts
  below every member a scoring channel reached. A routed page may lift a member no channel could
  score; it may not put one above a member a channel did. (The term is additive, so two scored
  members can still move relative to each other — the guarantee is about unscored pages only.)
  Measured as a peer ranking at a shared `k` it did the opposite, and cost the frozen gold set
  21/22 → 17/22. At its own constant routing is **on by default** (`ROUTE_BY_DEFAULT = True`,
  ADR 0019 Amendment 1): both gold arms scored 22/22 citing the same pages, so the channel
  cannot cost an answer, and it costs one live call. A request declines it with
  `tree_route=False` (then the answer is field for field the one a treeless service gives), a
  short label query is never routed, and a document with no tree has no third channel at all.
  No route is never an error: no budget, no transport, no usable reply and the channel is
  simply absent.
- **Metadata is verbatim, automatic and never a hard gate** (ADR 0013) — every page-metadata
  value quotes its page spans (dropped otherwise, with a diagnostic); the model runs only at
  build time, nobody annotates (lite ingest derives the candidate from text geometry instead —
  no region, page type `other`, so no cover / agenda exclusion — and verifies it the same way,
  ADR 0025); document metadata is a deterministic fold that is recomputed
  and refused on drift; only **period (by year) and region** pre-filter retrieval, regions
  only from the document's own vocabulary (no hardcoded company), and a filter that leaves
  fewer candidates than seats is relaxed and reported, never turned into an abstention.
- **Immutable, content-addressed snapshots** — `publish_draft` switches `current-*` pointers
  atomically and is idempotent; corrupted evidence is refused on every **read**, never served.
  Only a **write** that brings the bytes a digest names repairs it: `put` rewrites a missing /
  empty / truncated / other-digest object, a damaged stage-cache entry is a miss (recomputed,
  model calls replayed), a damaged source snapshot is re-extracted from its PDF, a lost model
  response is called once more; counts land in `DocumentRun.storage_repairs` ([ADR
  0029](../../docs/enterprise-pdf-rag/adr/0029-sharded-store-layout-and-self-healing.md)).
  Objects and stage-cache pointers live in `objects/sha256-sharded/<ab>/<digest>` and
  `stage-cache-sharded/<ab>/<fingerprint>`; the legacy flat `objects/sha256/` and
  `stage-cache/` are read (after the sharded place) and never written, so a full one (Workspace
  files: 10 000 children per folder) never blocks a write. They are created by hard link; where
  the filesystem cannot hard-link they fall
  back to check + rename + re-read (`ragspine.common.evidence.file_placement`, [ADR
  0020](../../docs/enterprise-pdf-rag/adr/0020-storage-without-hard-links.md): same bytes,
  first-writer-wins no longer atomic under concurrent writers). A mount verifies
  its **whole** pinned release once, when it is mounted — every asset digest, the source it was
  cut from, every member's evidence. Every later request re-reads the one file that names all
  of it, the pinned manifest object whose digest **is** the processing id, and refuses any
  drift: size and mtime only skip re-hashing a file nothing touched, a file that moved is
  re-hashed, and a mismatch falls through to the full mount-time verification, which refuses.
  Within one mount a member's evidence is hydrated once and a retrieval plan / index parsed
  once, both keyed by content. On the write side, each store **instance** verifies a snapshot
  once: `LocalDocumentStore` keeps the digests it has itself read back and hashed and the source
  manifests it has fully verified, so a verification-only sweep (`load`, `verify`, `publish`, a
  `put` of an object already on disk, `ProcessingStore.save_draft` / `load`'s asset sweep) skips
  what that instance already checked; bytes a caller consumes (`get`, `read_content`, the
  processing manifest object on every `ProcessingStore.load`) are always re-read and re-hashed,
  and every stage, scan and mount opens its own instance, so each re-checks the disk once ([ADR
  0024](../../docs/enterprise-pdf-rag/adr/0024-source-verification-cache.md)). Within a
  `shared_pdfs()` scope (`validate_processing_source`, `ProcessingRetrieval.build`, the ingest
  pipeline's `run`, `requalify_visual_objects`) a source PDF is read and opened once and shared by
  every table / formula / chart proof. `APP_VERIFY_EVERY_REQUEST=1` puts the full verification
  back on every request **and every store load** for an audit (an order of magnitude slower on a
  real document); the AIA source-review app always runs that way (`auditing()`).
- **Extraction: pdfspine as the only parser, garbled spans, same-SVG two branches, the two
  qualification scopes, grid-as-ink, model-free diagram / formula proofs, the one text
  criterion, purity** — moved with `documents/`, `figures/` and the extraction half of
  `processing/`; see
  [`src/ragspine/extraction/evidence/CLAUDE.md`](../ragspine/extraction/evidence/CLAUDE.md).
- **Settings, providers, credential isolation, local-model tunnel, live-LLM test policy** — moved
  with `core/` and the provider adapters; see
  [`src/ragspine/common/evidence/CLAUDE.md`](../ragspine/common/evidence/CLAUDE.md).
