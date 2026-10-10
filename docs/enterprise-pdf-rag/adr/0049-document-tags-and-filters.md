# ADR 0049: Document tags (year / region / category …) and an explicit tag filter

Status: Accepted, 2026-10-10. Additive: with no sidecar, no `APP_DOCUMENT_TAG_PATH_TEMPLATE` and
no filter nothing is written and nothing is filtered — every store, fingerprint, prompt, ranking
and answer is byte for byte what it was. It changes **no hash, no fingerprint, no envelope byte
and no published id**.

## Context

A user organises a few dozen PDFs by year, region and category and wants to ask questions of
one slice ("only 2024, only HK"). Physically every PDF already has its own library under
`<ingestion root>/<sha256>/`; splitting the ingestion root per slice would duplicate documents
that belong to several slices and change where every store lives. What is missing is a
**logical** label on each document and a way to narrow the candidate documents by it.

Two constraints shape the answer. Tags are user-given and change: re-tagging must never
re-ingest, so they cannot touch a content address. And narrowing must never be inferred from a
question set or a question's text — that would quietly turn evaluation labels into retrieval
hints (the `doc` field already only labels the evaluation since ADR 0032; the opt-in
`restrict_to_question_doc` is the comparison arm, not a default). A filter therefore comes only
from the caller, explicitly.

## Decision

1. **Sources** (`adapters/document_tags.py`; both read-only, the PDF folder is never written):
   - **Sidecar** `<PDF folder>/documents.csv` (CSV, chosen over JSON because the people keeping it
     edit it in a spreadsheet; UTF-8, a BOM tolerated). A `file` column — a path relative to the
     folder, or a bare file name that names exactly one PDF — plus any other columns, each one a
     tag; names are not fixed and every value is a string, stripped, a blank cell no tag. A missing
     `file` column, a repeated column name or two rows naming one PDF is a `ValueError` before any
     work; a row naming no PDF, or a bare name several PDFs share, tags nothing and is counted.
   - **Path template** `APP_DOCUMENT_TAG_PATH_TEMPLATE`, e.g. `{region}/{year}/{category}/{file}`:
     `{name}` placeholders (one path segment each, literals allowed around them, `{file}`
     required) full-matched against the PDF's folder-relative posix path. A PDF it does not
     match gets no tags from it and is counted; a malformed template is a `ValueError` before any
     work.
   - **Priority**: both merge key by key, the sidecar winning on a key both give.
   - Counts only (`tagged`, `untagged`, `sidecar_rows`, `sidecar_unmatched`,
     `sidecar_ambiguous`, `template_unmatched`) go to one `document_tags_resolved` trace and
     progress event — emitted only when a source exists. No path, no tag value.
2. **Storage**: one mutable record of the ingestion root, `document-tags.json`
   (`{"format": "document-tags-v1", "documents": {<source sha256>: {name: value}}}`), written
   through the object backend's `put_record` (atomic replace, last writer wins, file layout —
   the root holds no store db). Not a new per-document file: on a staged mount the document keeps
   its four files (ADR 0046). The folder is the source of truth: each run sets exactly the tags it
   read for its own documents (none clears them), leaves other documents' entries alone, and
   writes only when that changes the record. A duplicate PDF (same sha256) records the first
   path's tags; its own `DocumentRun.tags` still shows what its path read.
3. **Catalog**: `scan_catalog` attaches the record to `CatalogEntry.tags` (`document-catalog-v1`
   gains an optional `tags` object, default `{}` — additive). An unreadable record lists every
   document untagged with a warning and a `document_tags_unreadable` trace; it never fails a
   scan.
4. **Filter**: `{"year": ["2024", "2023"], "region": "HK"}` — values of one key OR, keys AND, a
   document without a filtered key never matches, comparison exact. `filter_catalog(catalog,
   filter)` narrows the **candidate document set** before anything is mounted; inside the kept
   documents retrieval, fusion, rerank, page windows, prompts and verification are untouched
   (a test pins the filtered answer's ranking to the one the kept documents give alone).
   `run_folder_pipeline(document_filter=…)` (CLI `run-folder --document-filter JSON`, notebook
   `NB_DOCUMENT_FILTER`, JSON string, `""` = none) applies it to the question phase only — every
   PDF is still ingested and tagged. `FolderPipelineResult.document_filter` /
   `documents_searched` and one `report.md` line record it. `HybridSearch` takes no filter: it
   ranks one corpus, and the corpus is what the filter already chose.
5. **Report**: `DocumentRun.tags`, and a `tags` column in `report.md`'s document table only when
   some document has tags. Citations keep `source_doc_id` / `document_sha256` + locator; tags
   never replace them.

## Consequences

- Re-tagging is a metadata write: the next run replays every stage (0 live calls, index reused,
  same processing id) and only `document-tags.json` changes.
- RESTRICTED filtering (ragspine's two exits) and the answer chain are untouched; the filter
  only removes whole documents before them.
- Not done: a per-request tag filter on `rag-chat-v1` / `serve` (a service mounts the whole
  catalog; a caller can pass `filter_catalog(scan_catalog(root), filter)` to
  `create_documents_app`). Case-insensitive or range matching (`year >= 2023`) is not offered.
