---
covers: src/enterprise_pdf_rag/
verified-against: 44c1896
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
   [ADR 0015](../../docs/enterprise-pdf-rag/adr/0015-diagram-and-formula-retrievable.md)
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
figures/      pure figure/chart pipeline — same rule; same-SVG two branches, snapshot binding
processing/   pure page-processing / qualification logic; context_builder.py (evidence blocks
              for the prompt), table_transcription.py (literal table transcription rule),
              geometry.py + table_grid_proof.py (the ruled-grid proof: every boundary, cell
              edge and merge bound to a real ruling — ADR 0014), diagram_models.py +
              diagram_description.py and formula_models.py + formula_rules.py (the model-free
              diagram / formula proofs and their deterministic projections — ADR 0015),
              index_text.py (contextual header + chart / diagram / formula projection both
              retrieval channels score), page_metadata.py /
              periods.py / document_metadata.py (verbatim page metadata, deterministic period
              forms, zero-model document fold — ADR 0013)
answers/      pure answer chain — ports.py (MountedDocument, MemberText), models.py
              (MemberFilters), prompt.py (strict model output schema), verify.py (claim
              re-read), query_filters.py / member_filter.py (period / region pre-filters
              derived from the question, relaxed when they starve); stdlib + pydantic only
adapters/     every SDK and I/O: pdfspine, http/ (FastAPI app factory; documents.py + chat.py
              serve document-catalog mode), local models, stores, draft_publication.py
              (qualify / index / publish), page_metadata_extraction.py (page_metadata stage),
              document_catalog.py (scan / mount), hybrid_search.py (BM25 + RRF + opt-in
              rerank borrowed from ragspine), answer_service.py (one model call per answer),
              diagram_geometry.py + diagram_qualification.py + diagram_publication.py and
              pdfspine_formula.py + formula_qualification.py (the ADR 0015 proofs, receipts
              and replays), visual_requalification.py (re-prove a saved snapshot's visual
              objects from its stored branches), chart QA v1/v2, nl_gold.py (the frozen
              natural-language gold set's schema and the judge both its runners share)
resources/    packaged prompts / static data
cli.py        enterprise-pdf-rag ingest|metadata|qualify|index|publish|serve|chart-qa|demo|extract|llm-smoke
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
  `<ingestion_root>/model-cache`, live budget `APP_ANSWER_MAX_LIVE_CALLS` (200).
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
  (ADR 0008 / 0009); values are never derived.
- **Verified claims only** — every model claim is re-read from stored evidence field by field
  (verbatim quote, exact cell text, chart value equal to the qualified `Decimal` or its exact
  source display, cited back to SVG elements);
  failed claims are dropped, and any number in the prose outside a verified claim abstains the
  whole answer (ADR 0011). One model call per answer; nothing is derived or retried.
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
- **Metadata is verbatim, automatic and never a hard gate** (ADR 0013) — every page-metadata
  value quotes its page spans (dropped otherwise, with a diagnostic); the model runs only at
  build time, nobody annotates; document metadata is a deterministic fold that is recomputed
  and refused on drift; only **period (by year) and region** pre-filter retrieval, regions
  only from the document's own vocabulary (no hardcoded company), and a filter that leaves
  fewer candidates than seats is relaxed and reported, never turned into an abstention.
- **Immutable, content-addressed snapshots** — `publish_draft` switches `current-*` pointers
  atomically and is idempotent; corrupted evidence is refused, never repaired. A mount re-reads
  its pinned manifest on every request and refuses drift.
- **pdfspine is the only PDF parser**; PNG wrapping is not structured extraction.
- **Credential isolation** — only the API subprocess inherits `EMBEDDING_*`; Open WebUI inherits
  no model key. Do not send real reports to external providers without task authorization.
