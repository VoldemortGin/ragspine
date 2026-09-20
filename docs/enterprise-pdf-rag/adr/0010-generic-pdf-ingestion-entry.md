# ADR 0010: Generic PDF ingestion through the existing evidence pipeline

Status: Accepted

## Context

The product accepts arbitrary PDF documents. AIA is a fixed acceptance corpus, not the platform's input identity or a global twenty-page limit. Existing source manifests preserve every source page; processing manifests select downstream pages. The current CLI binds ingestion and processing to the AIA profile and activates discovery pointers.

The user requested `scripts/enterprise_pdf_rag/ingest.py`, usable with a standard installed Python package as well as local uv. This slice adds an entry point, not a second extraction/model/index implementation. It does not claim arbitrary-document QA qualification.

## Decision

Expose `enterprise-pdf-rag ingest --pdf FILE [--pages all|1-3,5] [--output-dir PARENT] [--stage source|layout|semantics] [--max-live-calls N]`. `scripts/enterprise_pdf_rag/ingest.py` delegates to that public package command, with no path injection, uv subprocess, AIA constants, or duplicated ingestion business logic.

The public adapter `ingest_pdf` composes the existing pdfspine source extractor, source asset store, selected-page processing pipeline, model layout and independent semantic branches. Its default is `source`, zero model calls. Canonical source observations are saved for the selected pages; layout and semantic processing remain explicitly deferred. No retrieval index or model-generated summary substitutes for missing semantics.

The source manifest continues to store the complete PDF and every page. `--pages` selects Canonical/layout/semantic processing, not a partial source PDF. Help and result fields state this distinction. Physical pages are one-based; selection is checked against the actual source page count. The general domain has no twenty-page ceiling. Existing AIA commands keep their explicit first-twenty-page guards.

Outputs default to `APP_DATA_DIR/ingestion/<source-sha256>/`, or `<output-dir>/<source-sha256>/`, with separate `source` and `processing` stores. Different source bytes never share a document directory. Content-addressed manifests and existing successful-stage caches support repeat execution; corrupt/missing cached dependencies fail rather than silently substituting a new result. Reusing a source cache never re-extracts its page SVG/text observations.

No default or explicit ingestion stage changes `current-manifest`, `current-processing`, the existing AIA release, or the running services. The result includes exact store roots, source/processing IDs, selected physical pages, stage outcomes, review path, cache status and actual new model-call count. Artifacts form an immutable draft which another explicit workflow may qualify, index, review and publish.

`layout` and `semantics` use the existing configured JSON client and a shared explicit new-call budget; zero means cache-only. They do not infer provider credentials, enable automatic retries, or invoke embedding/reranking. Semantic adapters preserve independent same-source branches and PENDING/unknown/unavailable fields. This entry does not enable financial qualification for arbitrary charts. AIA layout corrections are excluded from the generic profile.

The existing installed-package configuration contract remains: a checkout has `.project-root`; outside a checkout set `APP_ROOT_DIR` to an existing work directory and optionally `APP_DATA_DIR`. The script never sources personal shell files or prints environment secrets.

## Changes and compatibility

- Generalize page bounds in `ProcessingScope` and `PageInput`, without adding identity fields or changing old content IDs.
- Add explicit deferred-layout, normalization-profile and draft-save options to `ProcessingPipeline`. Existing AIA defaults retain their behavior and published IDs.
- Reuse source persistence and review exporters; make the generic processing review title reflect the actual document.
- Keep the existing retrieval DAG and publication validators. The generic source stage has no eligible retrieval members.
- Preserve all p18/p20 work, raw assets, old snapshots and public ChartQA v1/v2 contracts.

## Verification and limits

Public-script and package-API tests use distinct authored PDFs, including a document with more than twenty pages. They check source identity/output isolation, selected Canonical pages, invalid paths/ranges, no implicit model calls or pointer activation, and deterministic successful-cache replay. Model-stage behavior uses offline boundaries; the default project gate never calls providers.

This does not add a generic web document catalog, upload endpoint, automatic serving of arbitrary ingested documents, unrestricted chat, universal chart qualification, or a new index implementation. Those capabilities require separate integration and acceptance. Non-PDF formats remain outside the current approved PRD.

Update (2026-09-19): the explicit follow-up workflow now exists as `qualify`/`index`/`publish` in `adapters/draft_publication.py` and the CLI, working by returned store/processing ids. Offline end-to-end coverage in `tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py` runs ingest→qualify→index→publish→retrieval on a non-AIA authored PDF, while a real local-embedder `index` still requires the managed tunnel and remains uncovered. This is a qualification/index/publication entry, not a general RAG answering chain; OpenAI-compatible chat still only does source review.
