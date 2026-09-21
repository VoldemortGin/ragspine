---
covers: src/enterprise_pdf_rag/
verified-against: a91ca6e
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
   [ADR 0018](../../docs/enterprise-pdf-rag/adr/0018-query-classification-and-translation.md)
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
core/         settings leaf + shared value types (no I/O)
documents/    pure document model — stdlib immutable values + Protocols only
figures/      pure figure/chart pipeline — same rule; same-SVG two branches, snapshot binding,
              source_label_match.py (the ADR 0016 window rule: one to three adjacent source
              occurrences, folded and concatenated, must equal the string being kept)
processing/   pure page-processing / qualification logic; context_builder.py (evidence blocks
              for the prompt, plus the uncitable page context block each hit is read
              beside — ADR 0017), table_transcription.py (literal table transcription rule),
              geometry.py + table_grid_proof.py (the ruled-grid proof: every boundary, cell
              edge and merge bound to a real ruling — ADR 0014), diagram_models.py +
              diagram_description.py and formula_models.py + formula_rules.py (the model-free
              diagram / formula proofs and their deterministic projections — ADR 0015),
              index_text.py (contextual header + chart / diagram / formula projection both
              retrieval channels score), page_metadata.py /
              periods.py / document_metadata.py (verbatim page metadata, deterministic period
              forms, zero-model document fold — ADR 0013)
answers/      pure answer chain — ports.py (MountedDocument, MemberText), models.py
              (MemberFilters, TranslatedQuery), prompt.py (strict model output schema),
              verify.py (claim re-read), page_window.py (one page context block per hit
              page, its members in the diagram reading order — ADR 0017),
              query_filters.py / member_filter.py (period / region pre-filters derived
              from the question, relaxed when they starve), query_mode.py (which
              retrieval channels a question uses — ADR 0018); stdlib + pydantic only
adapters/     every SDK and I/O: pdfspine, http/ (FastAPI app factory; documents.py + chat.py
              serve document-catalog mode), local models, stores, draft_publication.py
              (qualify / index / publish), page_metadata_extraction.py (page_metadata stage),
              document_catalog.py (scan / mount), hybrid_search.py (BM25 + RRF + opt-in
              rerank borrowed from ragspine; the channels a query uses are chosen, not
              fixed — ADR 0018), query_translation.py (restate a question written outside
              the index's language), answer_service.py (one synthesis call per answer),
              answer_audit.py (the local sqlite answer journal: one row per question, opened
              with the prompt as sent and closed with the verified result),
              diagram_geometry.py + diagram_qualification.py + diagram_publication.py and
              pdfspine_formula.py + formula_qualification.py (the ADR 0015 proofs, receipts
              and replays), visual_requalification.py (re-prove a saved snapshot's visual
              objects from its stored branches), chart QA v1/v2, nl_gold.py (the frozen
              natural-language gold set's schema and the judge both its runners share)
resources/    packaged prompts / static data
cli.py        enterprise-pdf-rag ingest|metadata|qualify|index|publish|serve|audit|chart-qa|demo|extract|llm-smoke
              + AIA-sample-only ingest-aia|process-aia-layout|process-aia-semantics|index-aia-processing
```

Structure is enforced by `scripts/enterprise_pdf_rag/check_conformance.py` (src layout, beartype
hook, absolute imports, closed import whitelist outside `adapters/`), `check_architecture.py`
(pure `figures/` `documents/` `processing/` `answers/`; only `answers/` may import pydantic),
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
  default empty). `EMBEDDING_*` enables search, `OPENAI_*` enables chat, `RERANK_*` enables
  opt-in rerank; a missing group is a 503 on its routes, never a mock. Model cache
  `<ingestion_root>/model-cache` — `requests/<fingerprint>.json` the record, `responses/`
  the bodies, `contexts/<fingerprint>.json` the **full request body as sent** (system rules,
  every message, schema, token budget, and the pinned sampling — it records
  `"temperature": 0.0, "seed": 0`; inline images summarized), written before the call and
  back-filled on replay, first write wins. It quotes the evidence verbatim: local files, never
  shared. Live budget `APP_ANSWER_MAX_LIVE_CALLS` (200), per-call wait
  `APP_ANSWER_TIMEOUT_SECONDS` (45, capped at 180 — a page window makes a "summarise this
  section" prompt long enough to need more), sampling seed `APP_ANSWER_SEED` (0; `temperature`
  is always `0.0`, a `seed` is sent only when one is configured — the sampling is inside the
  request body, so changing it misses every cached completion).
- **Answer journal:** every answer writes one row to `<ingestion_root>/answers-audit.sqlite` — the
  final `prompt_system` / `prompt_user` verbatim (opened *before* the call), the fused ranking with
  each channel's seat, then the raw model output, the verified / rejected claims and the status.
  `APP_ANSWER_AUDIT_ENABLED=false` writes nothing; `APP_ANSWER_AUDIT_PATH` moves the file. Read it
  back with `enterprise-pdf-rag audit --db <path> [--last N] [--fingerprint X] [--question-like …]
  [--show ID]` (no service, no model). It quotes the evidence verbatim: local file, never shared.
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
- **Source review ≠ semantic qualification** — only source-qualified facts reach ChartQA
  (ADR 0008 / 0009); values are never derived. A chart carries exactly one of **two named
  scopes**, and its receipt always says which: `explicit-distribution-shares` (native sector
  geometry plus complete source-paint accounting — ADR 0008) or the default
  `source-labels-and-verbatim-points-v1` (ADR 0016: a point survives only when its category
  **and** its value with unit each print verbatim inside the figure, fail-closed per point,
  never per figure; the category-to-value *association* stays the model's assertion, so the
  two scopes are named apart, stored apart and never merged). `figure-source-labels-only-v1`
  is frozen byte-for-byte so already-published members keep mounting and replaying; every gate
  accepts both, and `chart_publication.resolve_chart_member` re-derives a member under the
  scope its own receipt declares.
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
- **A verified table grid means ink** (ADR 0014) — `TableIR` / `TableCell` are `VERIFIED` only
  with `GridEvidence` / `CellBorderEvidence`: every row and column boundary sits on a real
  ruling, every cell edge is continuously ruled, every merge is proved by the absence of a rule
  inside it, and the whole proof is re-derived from the pinned source on every resolve. `row` /
  `col` / `header` citations open only for a verified grid, and `header` only names a header
  *proved* by a thick rule or a fill — never a font or first-row heuristic. An unruled, snapped
  or double-ruled table stays `PENDING`, and stays retrievable and citable by cell text.
- **A retrievable diagram or formula is proved without a model** (ADR 0015) — `DIAGRAM` and
  `FORMULA` objects reach the index only through a pure, replayable proof that runs beside (never
  inside) the two model branches: a node label must equal its cited span verbatim and its bbox must
  match a real painted frame, an edge needs a connector plus a filled arrowhead whose derived tip
  lands in the target, and every span inside the object must be cited; a formula token quotes a
  span substring under a tiling closure rule, a script is proved from the PDF's own `Ts` or marked
  `derived` (`proof_level="literal"`, `PENDING`), and every drawn path inside the object must be
  explained. One failed rule withholds the whole object with a verbatim diagnostic. Their
  `qualified_description` is a deterministic template, never a second model pass, and every resolve
  replays the proof from the pinned source. An `IMAGE` is still not retrievable.
- **One text criterion for names and symbols** — `verify._exact` (whitespace folded, case kept) is
  the single function behind the literal-transcription check, a cited table header (ADR 0014) and
  every diagram / formula claim, so the verify side is never laxer than the qualification side.
  Only cell *content* uses the case-folded `_norm`.
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
- **Same-SVG two branches, snapshot binding, no-summary-fallback** — hard invariants of the
  figure chain (ADR 0002). What gets embedded is the **index text** of
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
- **Metadata is verbatim, automatic and never a hard gate** (ADR 0013) — every page-metadata
  value quotes its page spans (dropped otherwise, with a diagnostic); the model runs only at
  build time, nobody annotates; document metadata is a deterministic fold that is recomputed
  and refused on drift; only **period (by year) and region** pre-filter retrieval, regions
  only from the document's own vocabulary (no hardcoded company), and a filter that leaves
  fewer candidates than seats is relaxed and reported, never turned into an abstention.
- **Immutable, content-addressed snapshots** — `publish_draft` switches `current-*` pointers
  atomically and is idempotent; corrupted evidence is refused, never repaired. A mount verifies
  its **whole** pinned release once, when it is mounted — every asset digest, the source it was
  cut from, every member's evidence. Every later request re-reads the one file that names all
  of it, the pinned manifest object whose digest **is** the processing id, and refuses any
  drift: size and mtime only skip re-hashing a file nothing touched, a file that moved is
  re-hashed, and a mismatch falls through to the full mount-time verification, which refuses.
  Within one mount a member's evidence is hydrated once and a retrieval plan / index parsed
  once, both keyed by content. `APP_VERIFY_EVERY_REQUEST=1` puts the full verification back on
  every request for an audit (an order of magnitude slower on a real document).
- **pdfspine is the only PDF parser**; PNG wrapping is not structured extraction.
- **Credential isolation** — only the API subprocess inherits `EMBEDDING_*`; Open WebUI inherits
  no model key. Do not send real reports to external providers without task authorization.
