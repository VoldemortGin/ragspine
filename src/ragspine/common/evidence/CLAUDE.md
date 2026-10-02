---
covers:
  - src/ragspine/common/evidence/
verified-against: 164b3ab
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
configs.py    APP_* settings leaf: env (prefix APP_) > <ROOT_DIR>/.env >
              config/enterprise-pdf-rag/settings.yaml; LLM / embedding / rerank / tunnel fields;
              notebook paths NB_PDF_DIR / NB_QUESTIONS_PATH / NB_REPORT_DIR (no APP_ prefix);
              ROOT_DIR / DATA_DIR / LOG_DIR; resource_path(package, relative)
settings.py   compatibility re-export of configs.py (old import path)
logging.py    the one logging config + lineage / privacy discipline for AI artifacts
providers/    providers.py (explicit APP_LLM_* / APP_EMBEDDING_* / APP_RERANK_* environment, opt-in
              connectivity smoke), json_completion.py (bounded JSON model calls, strict DTO
              validation, content-addressed immutable cache), local_models.py (embedding /
              rerank HTTP adapters), local_model_launcher.py (credential injection into one
              allowlisted child), local_model_tunnel.py (loopback-only SSH tunnel)
```

## Invariants (do not break)

- **`configs.py` is a leaf** — stdlib + pydantic / pydantic-settings only; it imports nothing
  from `ragspine.*` or `enterprise_pdf_rag` (`check_conformance.py`). It is deliberately **not**
  `ragspine/config/`: APP_* settings and `RAGSpineConfig` / `ServiceConfig` are two systems,
  don't merge them by accident.
- **`.env` lives at `ROOT_DIR`, never the CWD, and controlled children never read it** —
  real environment > `.env` > yaml > defaults; a missing `.env` is fine. A child started with
  `PYTHON_DOTENV_DISABLED=1` (launcher, `webui_preview`, the Open WebUI gate) skips `.env`, so
  its allowlisted environment, built by the parent from `Settings.as_environment`, is all it sees.
- **Only the three LLM settings fall back to `OPENAI_*`** — `llm_api_key` / `llm_base_url` /
  `llm_model` are declared `AliasChoices("APP_LLM_*", "OPENAI_*")`, so each falls back on its own
  when its `APP_LLM_*` name is unset (aliases carry no `env_prefix`; `populate_by_name` keeps
  `Settings(llm_model=...)` working). Inside one source `APP_LLM_*` wins; across sources the
  real environment still beats `.env` as a whole, so a shell `OPENAI_MODEL` outranks the
  `.env`'s `APP_LLM_MODEL` (pinned in `test_configs.py`). Embedding / rerank / tunnel have no
  fallback, `as_environment` still answers under `APP_LLM_*` only, and an injected mapping given
  to `load_llm_config` is taken as is. Controlled children never receive `OPENAI_*` (their
  allowlists carry the resolved `APP_LLM_*`), and Open WebUI's `OPENAI_API_KEY` placeholder
  lives in a child that never reads `Settings`.
- **`NB_*` path settings are unvalidated and APP_-less** — `pdf_source_dir` (`NB_PDF_DIR`),
  `questions_path` (`NB_QUESTIONS_PATH`), `report_dir` (`NB_REPORT_DIR`): `~` expanded, relative
  to `ROOT_DIR`, no existence check, blank means unset. Callers (`run_folder_pipeline`, the
  `run-folder` CLI) let an explicit argument win and raise only when a folder is needed.
- **Model / tunnel fields are lenient** — all optional strings / `SecretStr`; nothing is validated
  at import. `load_*_config` / `load_tunnel_config` validate (https, loopback, ports) only when that
  group is used, and still accept an injected mapping.
- **Project root is found, never guessed** — `ROOT_DIR` walks up from the CWD to the
  `.project-root` marker and raises if there is none; wheel / container deployments set
  `APP_ROOT_DIR` (must exist and be a directory; it need not contain the marker). Never fall
  back to `Path.cwd()`.
- **`resource_path()` takes the package name explicitly** — this module no longer lives in the
  package whose resources it locates, so it cannot infer it from `__name__`.
- **`logging.py` is not `common/observability`** — lineage logs are a separate concern;
  observability carries the privacy-aware-trace invariant, don't route one through the other.
- **Credential isolation** — only the API subprocess inherits `APP_EMBEDDING_*`; Open WebUI inherits
  no model key. `local_model_launcher` injects credentials into one allowlisted child
  environment only; the tunnel binds loopback only. Do not send real reports to external
  providers without task authorization.
- **A missing provider group is an error, never a mock** — no `APP_LLM_*` / `APP_EMBEDDING_*` /
  `APP_RERANK_*` means the routes that need it refuse (503), not a silent fallback.
- **Live LLM tests are separate and conservative** — run them explicitly only for a major
  release or a material change to the model-call flow; ordinary fixes use transport fakes.
  `llm-smoke` verifies connectivity only, it is not model-quality acceptance. The default gate
  never calls a model or network service.
