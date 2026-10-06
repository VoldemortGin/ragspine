# ADR 0032: A question is answered across every published document; `doc` only labels the evaluation

Status: Accepted, 2026-10-06. Amends [ADR 0022](0022-run-folder-question-docs-budget-and-progress.md)
(the answer routing of §1 / §2) and [ADR 0013](0013-page-metadata-and-prefilters.md) (the
`rag-chat-v1` title routing's 422). Nothing on disk changes: no store layout, fingerprint, cache
key, index text or artifact byte differs, and an ingestion directory written before this ADR is
reused as is. The answer journal gains one nullable column, added on open like `ranked`.

## Context

`run_folder_pipeline` asked a light question of exactly the one published document its `doc`
resolved to (ADR 0022). A `doc` that resolved to none or several, and a question without `doc`
over several mounted documents whose title words selected none, ended `routing_failed`: no
retrieval, no answer. In the field the question set's `doc` names matched none of the PDF file
names (how they differ is unknown), the notebook ran with `ONLY_QUESTION_DOCS = False`,
`QUESTION_SELECTION = "first"`, `ON_UNMATCHED_DOCS = "skip"` — the whole folder was ingested and
every question was `routing_failed`; `answers.csv` had an empty answer column. The user does not
want to maintain a mapping, and stated the requirement directly: a question is searched in every
PDF; the PDF a question names is for checking the answer afterwards, not for finding it.

## Decision

### 1. One corpus over every mounted document (`adapters/cross_document.py`)

`CrossDocument` satisfies the `MountedDocument` port over several mounted documents, so the
unchanged chain — hybrid search, ADR 0012 seats, the ADR 0017 page window, claim verification —
runs once over all of them:

- **Members** are the union of the documents' members. A member id is a content address, so two
  documents share one only for the byte-identical member; the first document by sha256 keeps it.
  Ids stay what every claim, citation and journal row already names.
- **Lexical channel**: one BM25 index over the union (cached by a corpus id derived from the
  documents' snapshot ids), so corpus statistics, and therefore scores, compare across documents.
- **Vector channel**: each document returns its own top `channel_limit`; the merge keeps the best
  `channel_limit` by score (one embedder, comparable cosine). The query is embedded once per
  catalog (`SharedQueryEmbedding`, a 64-entry memo around the injected embedder).
- **Tree channel** (ADR 0019): still one routing call per question — over the outline of the
  document owning the corpus's best BM25 member — and its pages are that document's members
  only (`HybridSearch.search(tree_members=…)`); a question BM25 cannot score is not routed.
- **Fusion** is the same RRF / `tree_rrf_k` on the merged rankings. Each channel offers no more
  candidates than for one document, the prompt keeps `top_k` seats and the same character
  budget, and a question still costs one synthesis call (plus the existing optional translation
  and tree route).
- Every fused hit is then **re-pinned to its own document's snapshot** (`FusedHit.document_sha256`);
  every read — `resolve`, `chart_context`, `displayed_context` — goes to that document.
- Pre-filters (ADR 0013) run over the union: the region vocabulary is the union of the
  documents', and a filter that starves the union is relaxed as before. The cover / agenda
  exclusion is per member and unchanged.

### 2. Provenance across documents

- The prompt names each block's document (`document=<cover title or file name> (<sha[:12]>)`,
  `ContextBlock.document`), and the page window is keyed by (document, page): page 3 of one
  report is never read beside page 3 of another (`with_page_context(documents=…)`).
- Verification is untouched and per member: a claim is re-read only against its member's own
  block, so quoting one document's figure while citing the other document's same-titled table
  is `claim_not_in_evidence`. `ClaimCitation.document_sha256` is filled from the member's owner,
  never from the model. The number guard and the prose gate are unchanged.
- `AnswerResult.document_sha256` (and the journal row's) names the document of the first prompt
  member; `searched_documents` lists every document searched; each citation, member rank, page
  window and journalled ranked / fused hit carries its own `document_sha256`.

### 3. `rag-chat-v1`

Additive fields only (`docs/enterprise-pdf-rag/schemas/rag-chat-v1.json`): request
`cross_document: bool = false`; envelope `searched_documents`; `document_sha256` on each
citation, member rank and page window. A request with `cross_document: true` searches every
mounted document and may not also name one (422). A request naming no document over several
mounted ones keeps ADR 0013's title / year routing when it selects exactly one document; where it
selected none or several — formerly a 422 — the question is now answered across all of them.
Naming a document (field or model id) behaves exactly as before. The rendered citation list
prefixes a page with its document's short sha256 when the answer searched several documents.

### 4. run-folder

Every light question is asked with `cross_document: true` whenever more than one document is
published (one published document: asked of it, byte for byte as before). `doc` no longer
routes: its resolution still decides `only_question_docs` / `first_matched` / `on_unmatched_docs`
— ingest scope and selection, whose semantics are unchanged — and labels the evaluation:

- `EvalCase.routing` (`cross_document` / `doc` / `failed`), `searched_documents`,
  `cited_documents`; `expected_doc` (the one published document `doc` resolves to, else
  `None`) and `cited_doc_hit` (a verified citation landed in it; `None` without an expected
  document or citation). Across documents `page_rank` is judged inside `expected_doc` and not at
  all without one. `totals.cross_document` counts the cases; `report.md` states the scope.
- `routing_failed` remains only for a question that was not asked: nothing published, or
  `restrict_to_question_doc=True` — the ADR 0022 routing, kept off by default for comparison.
- Gold sets (`nl-answers-gold-v1`) are pinned to one document by design and keep routing to it.

The retrieval test bench follows: `cross_document`, `expected_doc`, `cited_doc_hit` columns (the
`routing_failed` flag column is gone, the diagnosis remains for unasked questions); across
documents every rank looks for the expected pages inside the expected document only, and is
`n/a` (counted as `expected_doc`) when there is none. The journal's `searched_documents` column
says a row was cross-document.

## Consequences

- The field run answers every question: the answer column has content, a question whose answer
  is in no PDF abstains, and `cited_doc_hit` is `n/a` for every question whose `doc` matches no PDF.
- More candidates compete for the same seats: a corpus of many similar reports adds retrieval
  noise, and a label query (`Revenue 1H26`) can match the same printed label in several
  reports. The seats are not widened (prompt size and cost stay flat); the model sees each
  block's document name, and verification guarantees that whatever is cited is in the cited
  document — the risk is an abstention or the other report's correct figure, never a figure
  cited to a document that does not print it.
- A per-document question set loses no recall it had before: the expected document is one of the
  documents searched. It can lose seats to other documents; `restrict_to_question_doc=True`
  measures that difference.
- `AnswerService` raises `AmbiguousDocument` exactly as before for a request naming no document
  without `cross_document` over several documents (the HTTP layer decides when to set it).
