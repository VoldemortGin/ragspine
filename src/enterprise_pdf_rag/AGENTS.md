# Project rules — enterprise_pdf_rag

Sibling package of `ragspine` in this repository (see `docs/adr/0021-merge-enterprise-pdf-rag-as-sibling-package.md`).
Layout, run commands and invariants: [`CLAUDE.md`](CLAUDE.md). Long-form docs, handoff, PRD and ADRs:
`docs/enterprise-pdf-rag/`.

- Python 3.12, uv src layout, package `enterprise_pdf_rag` (import name unchanged by the merge).
- User-approved PRD v0.2 and numbered ADRs (`docs/enterprise-pdf-rag/adr/`) define scope. Implement one behavioral TDD slice at a time.
- figures/ and documents/ use standard-library immutable values and Protocols only. SDKs and I/O belong in adapters/. Boundaries use strict Pydantic models.
- pdfspine is the only PDF parser. SVG fidelity and source completeness are explicit. PNG wrapping is not structured extraction.
- Explicit aia-source-review, offline-demo or production mode; never silently fall back to mock. Source review does not imply semantic QA qualification. Do not send real reports to external providers without task authorization.
- Only natural-language descriptions are embedded. Same-SVG branches, snapshot binding and no-summary-fallback are hard invariants.
- Run `make fmt` for safe fixes and formatting. `bash scripts/ci.sh` is the only read-only completion gate (its step 9 runs the four `scripts/enterprise_pdf_rag/check_*.py` guards); do not suppress lint/type/warning failures.
- During TDD, run the relevant offline tests first (`.venv/bin/python -m pytest tests/enterprise_pdf_rag/<area> -q`). After a substantial change and at every implementation-phase close, run the complete `bash scripts/ci.sh` tool/unit/offline-integration gate.
- Live LLM tests are separate and conservative: run them explicitly only for a major release or a material change to the model-call flow. Ordinary code, configuration and sanitization fixes use transport fakes and must not trigger live calls. The authorized initial connectivity smoke has already run; do not repeat it automatically.
- llm-smoke verifies connectivity only. It is not chart-model quality acceptance; real chart golden-set acceptance remains future work. The default gate must never call a model or network service.
- `data/samples` and `docs/enterprise-pdf-rag/samples` are owned by the sample-research worker during this task. `figures/` and `tests/enterprise_pdf_rag/figures/` are owned by the figures worker.
- Do not commit, push, publish or deploy unless requested.
