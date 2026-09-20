# ADR 0007 — Installable Python runtime and external application data

Status: Accepted for the Python 3.12 packaging slice, 2026-09-19.

## Context

Local development uses uv and its lockfile. A separate deployment environment must be able to install the backend with standard `pip install .` or its PEP 517 wheel, without uv, a source checkout, or a particular virtual-environment directory at runtime. The existing default dependency set omits pdfspine and the SVG renderer, and the reviewed AIA region catalog is read from a repository-relative path that is absent from the wheel.

## Decision

Declare pdfspine 0.10.0 and resvg-py 0.5.0 in the default PEP 621 dependency set. Require Pydantic >=2.12 for the already used strict `TypeAdapter.validate_json(extra=...)` API and pydantic-settings >=2.2 for `YamlConfigSettingsSource`. Keep development tools in the development group and retain the now-empty `pdf` and `processing` extras as compatibility aliases. This installation includes pdfspine's mandatory bundled `ocrspine-models` dependency, but does not enable its optional learned OCR/table inference stacks or download additional weights. Preserve the domain/adapter import boundary independently of which dependencies pip installs.

Ship the immutable reviewed region catalog as a package resource, loaded through `importlib.resources`; it has one authoritative copy. User PDFs, manifests, model responses, vectors, credentials and writable configuration remain external data, never package resources. `APP_ROOT_DIR` explicitly selects an existing deployment configuration/work directory without requiring a source marker. `APP_DATA_DIR` selects the data directory, including the AIA source and processing stores. No current-working-directory fallback or synthetic-data fallback is introduced.

Expose an installed console command to serve the existing configured API through uvicorn. It accepts host/port explicitly, defaults to loopback, and performs no ingestion or model calls. Source-checkout `scripts/enterprise_pdf_rag/start.sh` remains a local development convenience; installed runtime commands do not execute that script or locate `.venv`.

Open WebUI remains a separate upstream application with its own installation, data and restricted environment; it is not a default dependency or new extra. The legacy 0.6.5 release pins a Pydantic version incompatible with the backend's current API. The 0.11.3 metadata resolves with the backend on Python 3.12, but brings a large independent dependency graph including GPU packages; metadata resolution is not a clean UI startup validation. No backend dependency downgrade or global vendor installation is justified by this packaging request. This decision does not claim Databricks platform compatibility: the target Python version, network routing and persistent storage must be checked for the selected Databricks environment.

## Validation

Use behavioral regressions for package-resource access outside the checkout, external data-root selection and the installed server entry point. Build the wheel, install with pip into an isolated Python 3.12 environment, run `pip check`, and exercise imports, catalog access, a small SVG render and API construction without PDF parsing or model calls. Record local evidence separately from any future Databricks deployment. Finish with the complete offline `bash scripts/ci.sh` gate.
