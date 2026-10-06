# ADR 0022: run-folder resolves the questions' PDFs first, ingests only those, and shows how far it got

Status: Accepted, 2026-10-05. Amends `run_folder_pipeline` (`adapters/folder_pipeline.py`) and
the ingest budget of [ADR 0010](0010-generic-pdf-ingestion-entry.md). Nothing on disk changes:
no file name, layout, fingerprint, cache key or artifact byte differs, and an ingestion
directory written before this ADR is reused as is. With the new arguments at their defaults
the pipeline ingests, routes and answers exactly as before, except that (a) a question's
`doc` may now also match a PDF by its normalized name and (b) the per-PDF ceiling is higher.

Two further parts of the same incident — re-verifying the whole source snapshot on every
`load`, and the `.claim` a killed process leaves behind — are decided separately.

## Context

A user ran `notebooks/run_folder.ipynb` on Databricks over a read-only folder of encrypted
annual-report PDFs (several hundred pages each) with `MAX_QUESTIONS = 10`. The `run` cell ran
for more than 8 hours without finishing. Three things in this pipeline made that worse:

1. `max_questions` limited only the answers. Every PDF of the folder was ingested, although
   the first ten questions named one or two of them.
2. The per-PDF budget was capped at 200 live calls. A page costs one layout call, one page
   metadata call and two per chart-like object, so one round covered about 100 pages; the
   rest of the document was left `deferred` — and the document still ended `published` with
   `error=None`. Nothing said only part of it was searchable.
3. Between `document_start` and `document_done` of one PDF there was no output at all, for
   hours.

And the user asked the obvious follow-up: *can it actually find the PDF each question names —
and what if the name is a few letters off?* Until now a `doc` matched a file name or its stem
(any case) or a ≥ 12-character sha prefix, and that resolution ran only at answer time.

## Decision

### 1. One resolution of `doc` → PDF, before any work (`adapters/question_docs.py`)

Every reference of the questions this run asks is resolved against the folder's PDFs before
any ingest, model call, embedding probe or write. The same resolution decides what
`only_question_docs` ingests **and** where each answer is routed, so "ingested" ⇔ "routed".
Rules, first hit wins:

| rule | what matches |
| --- | --- |
| `alias` | an entry of `doc_aliases` (key compared normalized) |
| `exact` | the file name, any case |
| `stem` | the file name without extension, any case |
| `sha_prefix` | ≥ 12 hex characters of the content sha256 — the only rule that reads PDF bytes, and only for a reference that looks hexadecimal and named nothing by its name |
| `normalized` | NFKC (full width → half width), casefold, basename of a path, no `.pdf`, runs of blanks / `_` / `-` / `.` as one separator |

A rule that hits several PDFs of different content is **ambiguous** and does not match (the
same bytes under two names are one document). **A near miss never matches**: annual-report file
names often differ in a year or "interim / annual" only, and the wrong one would be cited with
full provenance — a provenance violation dressed as a fix. Near misses are offered as
`candidates` (`difflib`, at most 3, with their similarity) for the user to write into
`doc_aliases`; an alias whose value names no PDF or several is refused before any work.

`check_question_docs(...)` exposes the same check read-only for the notebook's new
`question-docs` cell. The result (`QuestionDocsCheck`) is in `FolderPipelineResult.question_docs`
and `report.json`; the `question_docs_resolved` progress event carries counts and the
unmatched references.

### 2. `only_question_docs` and `on_unmatched_docs`

With a question set, `only_question_docs=True` ingests only the PDFs the asked questions name.
Every other PDF is `skipped_not_referenced` (a healthy status, no budget, `sha256=None` when its
bytes were never read — they are read only for a sha reference). Gold sets name documents by
`document_sha256`, so there every PDF is hashed once, and the hash is reused by the ingest loop.
A reference naming no PDF or several, or a light question without `doc`, is then a
`QuestionDocsError` with a Chinese explanation and the three ways out (`DOC_ALIASES`,
`ONLY_QUESTION_DOCS = False`, `ON_UNMATCHED_DOCS = "skip"`) — before any write or call
(`on_unmatched_docs="error"`, the default). `"skip"` leaves those questions out of the
selection; they are answered as `routing_failed` with the reason (questions without `doc` take
the existing no-`doc` routing). Without `only_question_docs` misses are recorded, never raised.
Ingesting only the asked pages of a PDF was rejected: it would publish a partial document and
does not remove the per-document costs that dominate on slow storage.

### 3. `question_selection="first_matched"`

`max_questions=N` with `"first_matched"` takes, in set order, the first N questions whose
references name exactly one PDF of the folder; the others are skipped without taking a seat and
listed with their reason in `QuestionSelection` (`report.json`, the notebook check). If the set
runs out, what was found is run and `short=True` says "only K questions of the set have their
PDF in the folder"; K = 0 is a `QuestionDocsError` before any work. This guarantees the PDF is
present, not that the answer is in it. `"first"` (the default of the function) is the old
behavior; `max_questions=None` ignores the mode. The notebook defaults to `"first_matched"`
and `ONLY_QUESTION_DOCS = True`.

> Update 2026-10-05: the notebook defaults are now `QUESTION_SELECTION = "first"`,
> `ONLY_QUESTION_DOCS = False`, `ON_UNMATCHED_DOCS = "skip"` (ingest the whole folder, take the
> first N questions, skip unresolved ones) because the question set's `doc` names do not yet
> map to the PDF file names and the whole folder is wanted anyway. Set `"first_matched"` /
> `True` / `"error"` to restore the behavior above. The library defaults are unchanged.

### 4. The per-PDF ceiling is 10 000, and `"auto"` follows the page count

The 200 ceiling came with the 20-page sample (ADR 0010) and was a typo guard, not a cost
model; nothing else depends on it. `MAX_INGEST_LIVE_CALLS = 10_000` (`pdf_ingestion.py`,
also enforced by `run_folder_pipeline` and the CLI); negative or larger values are still
refused before any work. Tree and answer budgets keep 200. The notebook's
`MAX_LIVE_CALLS_PER_PDF = 1000` covers ≈ 2 × pages + 2 × charts for a ~400-page report in one
round; it is a ceiling, calls are made only as needed and cache replays cost nothing.

`max_live_calls_per_pdf="auto"` (the notebook default, CLI `--max-live-calls-per-pdf auto`)
gives each PDF `min(MAX_INGEST_LIVE_CALLS, selected pages × AUTO_CALLS_PER_PAGE (4) +
AUTO_CALLS_BASE (50))` — 3 pages 62, 300 pages 1 250, capped from 2 488 pages on. Four per page
is one layout and one page-metadata call plus room for one chart-like object (two calls) per
page on average. The page count is the one the source stage already reads (`pages=` counts the
selected pages only), so no PDF is opened an extra time: `ingest_pdf` takes the budget as a
function of the selected page count and builds its model client after the source stage (the
LLM configuration is still loaded first, so a missing one fails as early as before). The
computed value is still taken from `max_live_calls_total`, and it is what `document_start`,
`document_progress` and `DocumentRun.live_call_budget` report; `document_start` is emitted
when it is known, before any model call. The tree budget (50 per document) stays fixed: the tree
summarizes branches of the outline, not pages, and its calls are bounded by the outline's size,
not the page count.

### 5. A partial ingest is visible

`published` keeps its meaning. `IngestionSummary` gains `pages_complete`,
`pages_budget_deferred` (a stage `deferred` or `call_budget_exhausted`) and
`pages_claim_blocked` (a stage refused with `request_in_progress_or_uncertain`), all defaulting
to 0 so older reports parse. They are in the `document_done` event (`pages="X/Y"`), the
`report.md` table and the notebook status table, which also prints "only X/Y pages, raise the
budget and run again".

### 6. Page progress

`document_progress` events: for the `layout` and `metadata` ingest stages, the first and last
page of the stage and in between at most one event per 10 pages or 30 seconds, with
`pages_done`, `pages_total`, `live_calls`, `budget` and `cache_hits` (model-cache replays;
a page replayed from the stage cache asks the model nothing and is not counted); one event per
later stage (`requalify`, `qualify`, `index`, `publish`, `tree`). Progress events are the
notebook's `progress` callback, not traces: they carry identifiers and counts, and the
question-doc events carry `doc` spellings and PDF file names — the user's own metadata, which
they need to fix a mismatch — never a question, an answer or page text, and none of it reaches
`emit_trace` or a log.

## Consequences

- The run cell can no longer start ingesting while a question's PDF is missing: with the
  notebook defaults that is reported by the `question-docs` cell before anything else runs.
- A rerun with `only_question_docs=False` ingests the rest of the folder; already ingested PDFs
  replay from cache (0 live calls).
- `DocumentRun.sha256` is `str | None`; `None` only for a skipped PDF never read.
- CLI: `--only-question-docs` and the new ceiling; `question_selection`, `doc_aliases` and
  `on_unmatched_docs` stay function / notebook only, like `max_questions`.
