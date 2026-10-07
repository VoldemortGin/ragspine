---
covers:
  - src/ragspine/common/evidence/
verified-against: 5dd8418
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
              notebook paths NB_PDF_DIR / NB_QUESTIONS_PATH / NB_REPORT_DIR (no APP_ prefix; the notebook
              itself no longer reads NB_REPORT_DIR, it pins reports under data/reports/);
              PDF_INGEST_PASSWORD (no APP_ prefix) for password-protected PDFs;
              ROOT_DIR / DATA_DIR / LOG_DIR; resource_path(package, relative);
              onnx_layout_model (APP_ONNX_LAYOUT_MODEL — the local PP-DocLayoutV3 weights
              for the "onnx-layout" partitioner, enterprise-pdf-rag ADR 0030);
              verify_persisted_receipts (APP_VERIFY_PERSISTED_RECEIPTS, default true — reuse
              a store's persisted verification receipts, enterprise-pdf-rag ADR 0034)
settings.py   compatibility re-export of configs.py (old import path)
logging.py    the one logging config + lineage / privacy discipline for AI artifacts
file_placement.py  link_new_file (create-if-absent by hard link; rename + re-read fallback where
              the filesystem cannot hard-link, e.g. Databricks FUSE) + fsync_directory (skips
              "unsupported" errnos); shared by the model cache and enterprise_pdf_rag's stores,
              docs/enterprise-pdf-rag/adr/0020-storage-without-hard-links.md; plus the ADR 0029
              sharded layout (`sharded_path` = `<dir>-sharded/<ab>/<name>`, `stored_path` /
              `read_stored` sharded first then flat, `stored_names` both) and the repair ledger
              (`recording_repairs` / `note_repair`, a context variable, counts only)
object_backend/  the store persistence seam (sqlite object-store PR-1): ObjectBackend /
              ModelCacheBackend protocols + typed errors (protocol.py), FileBackend /
              FileModelCacheBackend carrying today's three placement paths byte-identically
              (files.py), SqliteBackend / SqliteModelCacheBackend (one WAL db per store root,
              zlib-inlined small objects, external large objects via the sharded layout,
              read-through of every legacy generation, O_EXCL writer lease instead of FUSE
              file locks — sqlite.py), the per-directory availability probe whose failure
              codes never carry a path (probe.py), the generalized ADR 0023 writer lease
              (lease.py; json_completion keeps its own copy until PR-3), and
              open_backend(root, kind) (registry.py: files | sqlite — probe failure raises,
              never silently falls back | auto). **Not wired yet**: no store or model-cache
              call site reads it until PR-2/3, so behavior and bytes are unchanged; the
              APP_OBJECT_STORE_* settings in configs.py are inert until then. Digests /
              fingerprints / envelope bytes / published ids are invariant across backends
              (tests/enterprise_pdf_rag/object_backend/). ADR: docs/enterprise-pdf-rag/adr/
              00NN-sqlite-object-backend.md.
providers/    providers.py (explicit APP_LLM_* / embedding / APP_RERANK_* environment, opt-in
              connectivity smoke), json_completion.py (bounded JSON model calls, strict DTO
              validation, content-addressed immutable cache), local_models.py (embedding /
              rerank HTTP adapters; `embed_descriptions` batches index texts, 16 inputs /
              48 000 chars, halving on failure — enterprise-pdf-rag ADR 0026 — after a
              transient failure's retries are spent, ADR 0035), transient.py (ADR 0035:
              `TRANSIENT_HTTP_STATUSES` / `TRANSIENT_FAILURE_CODES`, jittered backoff,
              the process-wide `Retry-After` cooldown per (URL, model); `_sleep` / `_clock` /
              `_random` are the test seams),
              local_model_launcher.py (credential injection into one
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
- **The three LLM settings and the embedding model use `OPENAI_*` as the preferred name** — `llm_api_key` / `llm_base_url` /
  `llm_model` are declared `AliasChoices("OPENAI_*", "APP_LLM_*")`, so each reads its own
  `OPENAI_*` name first and the `APP_LLM_*` alias otherwise (aliases carry no `env_prefix`;
  `populate_by_name` keeps `Settings(llm_model=...)` working). Inside one source `OPENAI_*` wins; across
  sources the real environment still beats `.env` as a whole, so a shell `APP_LLM_MODEL` outranks the
  `.env`'s `OPENAI_MODEL` (pinned in `test_configs.py`). `embedding_model` reads
  `OPENAI_EMBEDDING_MODEL` then `APP_EMBEDDING_MODEL`; rerank / tunnel have no alias.
  `as_environment` still answers under `APP_*` names only, and an injected mapping given
  to `load_llm_config` is taken as is. Controlled children never receive `OPENAI_*` (their
  allowlists carry the resolved `APP_LLM_*`), and Open WebUI's `OPENAI_API_KEY` placeholder
  lives in a child that never reads `Settings`.
- **Embedding shares the LLM gateway by default** — without `APP_EMBEDDING_BASE_URL` (blank counts
  as unset), `load_local_model_config("embedding")` uses `APP_LLM_BASE_URL` (https only), the
  `APP_EMBEDDING_API_KEY` if set else the LLM key, and the required embedding model (error names
  `OPENAI_EMBEDDING_MODEL`). Any other `APP_EMBEDDING_BASE_URL` is a separate service: loopback,
  own model and key. A base equal to the LLM base counts as the gateway, because
  `as_environment` hands children the **resolved** `APP_EMBEDDING_*` (gateway base and key), so
  a child also needs the non-secret `APP_LLM_BASE_URL` (the `aia-source-review` API allowlist
  carries it). Rerank has no gateway fallback. Fingerprint stays `local-http/<model>`.
- **`NB_*` path settings are unvalidated and APP_-less** — `pdf_source_dir` (`NB_PDF_DIR`),
  `questions_path` (`NB_QUESTIONS_PATH`, alias `DATASET_PATH` — primary wins, a blank primary falls back), `report_dir` (`NB_REPORT_DIR`): `~` expanded, relative
  to `ROOT_DIR`, no existence check, blank means unset. Callers (`run_folder_pipeline`, the
  `run-folder` CLI) let an explicit argument win and raise only when a folder is needed.
  `notebooks/run_folder.ipynb` does not read `report_dir` at all (reports go to `data/reports/<stem>/`).
- **`PDF_INGEST_PASSWORD` is APP_-less and secret** — `pdf_ingest_password` (`SecretStr`, blank means
  unset), the same name as SuperIndex's. Only `enterprise_pdf_rag.adapters.pdf_password.open_pdf` reads it;
  it never enters a message, report, trace or the ingestion directory. `as_environment` cannot answer it
  (not `APP_*`), so `webui_preview` hands it to the `document-catalog` API child by name.
- **`OPENAI_TEMPERATURE` (alias `APP_LLM_TEMPERATURE`) is validated at load** — `llm_temperature` is a raw
  string; `load_llm_config` turns unset / blank into `0.0`, a number in [0, 2] into itself and `omit` into
  `LLMConfig.temperature = None` (no field sent), anything else into `ProviderConfigurationError`.
- **A provider error body is read only for a 400, and only two fields are kept** — `_send_once` reads ≤ 4096
  bytes, keeps the checked `error.param` / `error.code` (`^[A-Za-z0-9_.\[\]-]{1,64}$`) and drops the rest
  unretained; the message never reaches a record, exception, log or trace. Other statuses read no body;
  of a non-200's headers only `retry-after-ms` / `Retry-After` is read (ADR 0035).
  `JsonCompletionClient` drops a refused `temperature` / `seed` (`DEGRADABLE_SAMPLING_PARAMETERS`; code
  `unsupported_value` / `unsupported_parameter`) and resends under the dropped body's own fingerprint;
  refusals are remembered per (URL, model) in process and on disk (`provider_error_param` on the 400 record,
  `sampling_parameter_unsupported` skip records), and a pre-ADR param-less 400 record gets one re-probe at
  `.retry-1.json`. Inside a `one_sampling_probe()` scope (held by `run_folder_pipeline` while PDFs run
  in parallel, enterprise-pdf-rag ADR 0033) the first call to an (URL, model) is sent alone and racing
  first calls wait for it; outside any scope nothing waits. `cache_hit_count` counts the calls a client answered from the model cache (the
  run-folder `document_progress` event reports it; enterprise-pdf-rag ADR 0022). Records stay byte-identical unless a 400 body was examined (`exclude_unset`).
  [ADR 0021](../../../../docs/enterprise-pdf-rag/adr/0021-sampling-parameter-fallback.md).
- **A model-call claim blocks only while its holder may run** — `requests/<fp>.json.claim` is created
  `O_EXCL` before transport and holds `{claim: json-completion-claim-v2, host, pid, process token,
  created_at, lease_seconds = ceil(4·timeout+31)+120}` (never a body); it is deleted once the record is written, and
  a caller re-checks the record after acquiring a claim (replay, never a second send). A holder that is a
  gone pid on this host (POSIX only), or past its lease, or a legacy claim (fingerprint / empty) whose mtime
  is > `LEGACY_CLAIM_LEASE_SECONDS` (900) old, is taken over by `O_EXCL` of `<claim>.takeover-<n+1>` —
  never `link_new_file` / rename / replace; one live call, record marked `diagnostics.claim_takeover`.
  `claim_blocked_count` / `claims_taken_over` count both outcomes. Lease clock `_wall_clock` is the test seam.
  [ADR 0023](../../../../docs/enterprise-pdf-rag/adr/0023-claim-takeover.md). Since ADR 0035 the lease
  covers one `RETRY_MAX_PAUSE` (31 s) too (331 s / 871 s) and a holder renews it before each transient
  retry by creating the next `.takeover-<n>` itself (losing that create ends the call unsent).
- **A transient provider failure is retried, never cached** — HTTP 408 / 429 / 500 / 502 / 503 / 504,
  timeout and connection errors are retried within the call (≤ 3 retries, `min(30, 2^n)` s with equal
  jitter, `Retry-After` honoured and shared per (URL, model); each retry a live call against
  `max_live_calls`); one that still fails writes **no** record (`retry_count` / `transient_failure_count`),
  so the next run calls it again. A record of such a code written before is called again (not replayed)
  and replaced unless it fails transiently again; `cache_only` still replays it. Permanent failures
  (400 / 401 / 403 / 404 / 413 / 422, invalid replies) are recorded and replayed as before. Embedding
  requests retry the same way before ADR 0026 halves a batch.
  [ADR 0035](../../../../docs/enterprise-pdf-rag/adr/0035-transient-provider-errors.md).
- **A damaged model-cache entry is called again once, never stuck** — a record that does not
  parse, or a success record whose response is missing / not its digest, is re-sent under a
  claim (budgeted; `cache_only` still raises `invalid_cache_record` / `missing_cached_response`)
  and repaired: the response by replace, the record only when the new response differs. A failed
  repair writes no failure record. `repaired_count`; a record bound to another fingerprint is still
  `cache_binding_mismatch`. The model cache itself stays flat (see ADR 0029 for the counts).
  [ADR 0029](../../../../docs/enterprise-pdf-rag/adr/0029-sharded-store-layout-and-self-healing.md).
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
- **Credential isolation** — only the API subprocess inherits `APP_EMBEDDING_*` (in gateway mode that
  carries the LLM key under `APP_EMBEDDING_API_KEY`); Open WebUI inherits
  no model key. `local_model_launcher` injects credentials into one allowlisted child
  environment only; the tunnel binds loopback only. Do not send real reports to external
  providers without task authorization — note that gateway-mode embedding (no
  `APP_EMBEDDING_BASE_URL`) sends chunk and query text to the LLM gateway.
- **A missing provider group is an error, never a mock** — no `APP_LLM_*` / embedding /
  `APP_RERANK_*` means the routes that need it refuse (503), not a silent fallback.
- **Live LLM tests are separate and conservative** — run them explicitly only for a major
  release or a material change to the model-call flow; ordinary fixes use transport fakes.
  `llm-smoke` verifies connectivity only, it is not model-quality acceptance. The default gate
  never calls a model or network service.
