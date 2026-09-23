---
covers:
  - src/ragspine/common/evidence/
verified-against: e831b0b
---

# common/evidence — agent contract

Auto-loaded when working under `src/ragspine/common/evidence/`. Cross-cutting config and model
access for the traceable PDF evidence chain (the enterprise-pdf-rag product line, moved here
from `enterprise_pdf_rag.{core, adapters}` by
[ADR 0022](../../../../docs/adr/0022-dissolve-enterprise-pdf-rag-into-domain-evidence-subtrees.md);
the legacy names still import, as the same module objects, with a `DeprecationWarning`).
Long-form docs: `docs/enterprise-pdf-rag/` (`local-models.md`, `testing-and-ingestion.md`).

## What lives here

```
settings.py   APP_* settings leaf: env (prefix APP_) + config/enterprise-pdf-rag/settings.yaml;
              ROOT_DIR / DATA_DIR / LOG_DIR; resource_path(package, relative)
logging.py    the one logging config + lineage / privacy discipline for AI artifacts
providers/    providers.py (explicit OPENAI_* / EMBEDDING_* / RERANK_* environment, opt-in
              connectivity smoke), json_completion.py (bounded JSON model calls, strict DTO
              validation, content-addressed immutable cache), local_models.py (embedding /
              rerank HTTP adapters), local_model_launcher.py (credential injection into one
              allowlisted child), local_model_tunnel.py (loopback-only SSH tunnel)
```

## Invariants (do not break)

- **`settings.py` is a leaf** — stdlib + pydantic / pydantic-settings only; it imports nothing
  from `ragspine.*` or `enterprise_pdf_rag` (`check_conformance.py`). It is deliberately **not**
  `ragspine/config/`: APP_* settings and `RAGSpineConfig` / `ServiceConfig` are two systems,
  don't merge them by accident.
- **Project root is found, never guessed** — `ROOT_DIR` walks up from the CWD to the
  `.project-root` marker and raises if there is none; wheel / container deployments set
  `APP_ROOT_DIR` (must exist and be a directory; it need not contain the marker). Never fall
  back to `Path.cwd()`.
- **`resource_path()` takes the package name explicitly** — this module no longer lives in the
  package whose resources it locates, so it cannot infer it from `__name__`.
- **`logging.py` is not `common/observability`** — lineage logs are a separate concern;
  observability carries the privacy-aware-trace invariant, don't route one through the other.
- **Credential isolation** — only the API subprocess inherits `EMBEDDING_*`; Open WebUI inherits
  no model key. `local_model_launcher` injects credentials into one allowlisted child
  environment only; the tunnel binds loopback only. Do not send real reports to external
  providers without task authorization.
- **A missing provider group is an error, never a mock** — no `OPENAI_*` / `EMBEDDING_*` /
  `RERANK_*` means the routes that need it refuse (503), not a silent fallback.
- **Live LLM tests are separate and conservative** — run them explicitly only for a major
  release or a material change to the model-call flow; ordinary fixes use transport fakes.
  `llm-smoke` verifies connectivity only, it is not model-quality acceptance. The default gate
  never calls a model or network service.
