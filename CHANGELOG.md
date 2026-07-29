# Changelog

All notable changes to RAGSpine are documented here. This project follows Semantic Versioning.

## [Unreleased]

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

[Unreleased]: https://github.com/VoldemortGin/ragspine/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/VoldemortGin/ragspine/compare/v0.11.0...v0.12.0
