# Changelog

All notable changes to RAGSpine are documented here. This project follows Semantic Versioning.

## [Unreleased]

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

[Unreleased]: https://github.com/VoldemortGin/ragspine/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/VoldemortGin/ragspine/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/VoldemortGin/ragspine/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/VoldemortGin/ragspine/compare/v0.11.0...v0.12.0
