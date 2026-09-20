---
covers: src/enterprise_pdf_rag/
verified-against: 407849e
---

# enterprise_pdf_rag — agent contract

Auto-loaded when working under `src/enterprise_pdf_rag/`. Keep terse; the long-form docs live
in `docs/enterprise-pdf-rag/`. This is a **sibling package** of `ragspine` in the same repo and
the same `pyproject.toml` — import name unchanged, not under `ragspine.*`
([ADR 0021](../../docs/adr/0021-merge-enterprise-pdf-rag-as-sibling-package.md)).

## Read first (in order)

1. [`AGENTS.md`](AGENTS.md) — project rules (scope, one TDD slice at a time, offline default).
2. [`docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md`](../../docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md) —
   the top “并入 rag-spine 记录（2026-09-20）” and “当前后续工作顺序” win over the historical
   snapshots kept below them.
3. [ADR 0009](../../docs/enterprise-pdf-rag/adr/0009-source-qualified-expense-ratio-bar-lookup.md),
   [ADR 0010](../../docs/enterprise-pdf-rag/adr/0010-generic-pdf-ingestion-entry.md) and
   [PRD v0.2](../../docs/enterprise-pdf-rag/PRD-v0.2.md) define scope; the full list is
   [`docs/enterprise-pdf-rag/adr/`](../../docs/enterprise-pdf-rag/adr/).
4. [`testing-and-ingestion.md`](../../docs/enterprise-pdf-rag/testing-and-ingestion.md) — what is
   actually testable today. Source review or an HTTP 200 is **not** a finished generic RAG.

## What lives here

Traceable financial-PDF RAG backend: content-addressed **immutable snapshots**
(source → processing → retrieval), an **evidence chain** from PDF span/drawing to answer, and
**source-qualified ChartQA**. Natural-language answering is not implemented yet — wiring it to
`ragspine`'s `HybridRetriever` / rerank / agent chain is the next step (ADR 0021).

```
core/         settings leaf + shared value types (no I/O)
documents/    pure document model — stdlib immutable values + Protocols only
figures/      pure figure/chart pipeline — same rule; same-SVG two branches, snapshot binding
processing/   pure page-processing / qualification logic
adapters/     every SDK and I/O: pdfspine, http/ (FastAPI app factory), local models, stores,
              draft_publication.py (qualify / index / publish), chart QA v1/v2
resources/    packaged prompts / static data
cli.py        enterprise-pdf-rag ingest|qualify|index|publish|serve|chart-qa|demo|extract|llm-smoke
              + AIA-sample-only ingest-aia|process-aia-layout|process-aia-semantics|index-aia-processing
```

Structure is enforced by `scripts/enterprise_pdf_rag/check_conformance.py` (src layout, beartype
hook, absolute imports, closed import whitelist outside `adapters/`), `check_architecture.py`
(pure `figures/` `documents/` `processing/`), `check_schema.py`
(`docs/enterprise-pdf-rag/schemas/*.json` ⇄ pydantic boundary models) and `check_drift.py`.

## Run (always from the repo root)

- **Tests:** `.venv/bin/python -m pytest tests/enterprise_pdf_rag -q` — the suite's own
  `conftest.py` enforces no network. It is also collected by the repo-wide run.
- **Gate:** `bash scripts/ci.sh` — step 5 runs this suite, step 9 the four checks above.
  The old `./ci.sh` no longer exists.
- **Config:** `config/enterprise-pdf-rag/settings.yaml`. **Runtime data:** `data/`
  (gitignored; APFS-cloned from the original repo — never clean or overwrite `data/output`,
  `data/validation`, `data/samples` or the `current-*` pointers).
- **Deploy:** `deploy/enterprise-pdf-rag/open-webui/` — `backend.Dockerfile` is not re-verified
  since the merge (local `../corespine` uv source; see ADR 0021 follow-ups).
- **Benchmarks / gold:** `benchmarks/enterprise-pdf-rag/aia-2026-interim/`.

## Invariants (do not break)

- **Explicit mode, never silent mock** — `aia-source-review` / `offline-demo` / production is
  chosen explicitly; the default gate calls no model or network service.
- **Source review ≠ semantic qualification** — only source-qualified facts reach ChartQA
  (ADR 0008 / 0009); values are never derived.
- **Same-SVG two branches, snapshot binding, no-summary-fallback** — hard invariants of the
  figure chain (ADR 0002). Only natural-language descriptions are embedded.
- **Immutable, content-addressed snapshots** — `publish_draft` switches `current-*` pointers
  atomically and is idempotent; corrupted evidence is refused, never repaired.
- **pdfspine is the only PDF parser**; PNG wrapping is not structured extraction.
- **Credential isolation** — only the API subprocess inherits `EMBEDDING_*`; Open WebUI inherits
  no model key. Do not send real reports to external providers without task authorization.
