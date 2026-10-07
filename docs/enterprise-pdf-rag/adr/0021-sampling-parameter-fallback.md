# ADR 0021: A configurable temperature, and dropping a sampling parameter the endpoint refuses

Status: Accepted, 2026-10-04. Amends the request body pinned by
[ADR 0018](0018-query-classification-and-translation.md) (`temperature: 0.0` on every
completion, `seed` when configured), the model-cache failure records of
[ADR 0011](0011-document-catalog-and-verified-answer-chain.md) (`retry_failed=False`: a failed
live call is cached and replayed), and the rule that a provider's error body is never read
(`providers.ProviderRequestError`, `JsonCompletionError`). On an endpoint that accepts every
parameter, nothing changes: same request bytes, same fingerprints, same record bytes, so every
existing cache entry still hits.

> Amended by [ADR 0033](0033-parallel-documents.md) (2026-10-06): inside a
> `one_sampling_probe()` scope, which `run_folder_pipeline` holds while PDFs ingest at once, the
> first call to an (endpoint, model) is sent alone and concurrent first calls wait for it, so the
> in-process memory below is learned from one probe per run. Outside any scope nothing waits.
>
> Amended by [ADR 0035](0035-transient-provider-errors.md) (2026-10-06): a failure record is no
> longer written for a **transient** failure (HTTP 408 / 429 / 500 / 502 / 503 / 504, timeout,
> connection error). Such a failure is retried within the call (jittered exponential backoff,
> `Retry-After` honoured — `_send_once` now reads that header, still no non-400 body; every
> retry a live call), and if it still fails the call leaves no record, so the next run calls it
> again. A `provider_http_429` / `provider_timeout` / ... record written before is called again
> instead of replayed and replaced by the outcome. "Every failure record is a permanent negative
> cache" below now holds for permanent failures only; the 400 refusal logic is unchanged.

> Amended by [ADR 0036](0036-sqlite-object-backend.md) §8 (2026-10-06, PR-3): the model cache
> may live in `model-cache.sqlite` instead of `requests/` files. A record is addressed by its
> key — `<fp>` or `<fp>.retry-1` — in either backend, with the same bytes; "no skip record
> under a claim" asks the backend (`claimed(key)`: a `claims` row, or the `.claim` file as
> before, legacy files included on sqlite). Every rule of this ADR is unchanged and its tests
> run on both backends.

## Context

A user ran `notebooks/run_folder.ipynb` on Databricks against Azure OpenAI
(`https://<resource>.openai.azure.com/openai/v1/chat/completions`, a reasoning model). Every
request of the pipeline, in all four shapes (ingest text, ingest vision, tree, answer with
`seed`), was refused with HTTP 400:

```json
{"error": {"message": "Unsupported value: 'temperature' does not support 0.0 with this model. Only the default (1) value is supported.",
           "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}
```

The notebook's `llm-selfcheck` bisected the cause: the same bodies without `temperature`
(strict `json_schema`, `max_completion_tokens`, `stream: false`, `image_url`, `seed` kept) all
pass. Three things made this worse than one bad parameter:

1. `temperature` was hard-coded; there was no way to stop sending it.
2. The transport deliberately never read an error body, so the failure record said only
   `provider_http_400`; finding the cause took an external tool.
3. Every failure record is a permanent negative cache. A rerun replays the 400 without a
   request, so fixing the configuration alone would not have helped: each PDF's
   `processing/model-cache/requests/` already held one such record (plus its `.claim`) per call,
   and the user reruns without cleaning `data/`.

## Decision

### 1. `OPENAI_TEMPERATURE` (alias `APP_LLM_TEMPERATURE`)

`Settings.llm_temperature` stores the raw string, lenient like every model field;
`load_llm_config` validates it into `LLMConfig.temperature`:

| Value | Request body |
|---|---|
| unset or blank | `"temperature": 0.0` (as before) |
| a number in `[0, 2]` | `"temperature": <number>` |
| `omit` (any case) | no `temperature` field (the endpoint's own default) |
| anything else (`none`, `default`, `nan`, `-0.1`, `2.5`, ...) | `ProviderConfigurationError` naming `OPENAI_TEMPERATURE` |

A blank value means "unset" everywhere in this repository's `.env`, so "send none" needs an
explicit word; `omit` says what happens. The temperature is part of the body and so of the
request fingerprint. Every `JsonCompletionClient` takes it from its `LLMConfig`, which is how it
reaches every chat body on the `run_folder_pipeline` path (ingest layout / metadata / semantics,
tree, answer, query translation, tree routing) and the `document-catalog` service
(`webui_preview` hands `APP_LLM_TEMPERATURE` to the API child). `OpenAICompatibleSmoke` never
sent a temperature. The ragspine main-chain providers (`agent/`, corespine's OpenAI provider)
are separate implementations that do not use `LLMConfig` and are unchanged.

`answer_seed` stays as it is: `null` in `settings.yaml` already sends no seed; there is no
`.env` spelling for it.

### 2. The transport reads `error.param` / `error.code` of a 400, and nothing else

On HTTP **400 only**, `_send_once` reads at most 4096 bytes of the body (a 4097th byte means
oversized), parses it as JSON, and keeps `error.param` and `error.code` only if each is a string
matching `^[A-Za-z0-9_.\[\]-]{1,64}$`. The bytes are then dropped. Anything else — not JSON,
too deep, not UTF-8, no `error` object, a non-string or out-of-charset field — yields no field.
`ProviderRequestError` carries them as `param` / `error_code`; its message is unchanged. Every
other status (401, 403, 404, 429, 5xx), a timeout and a connection error still read nothing.

The provider's `message`, and every other byte, never reaches a record, a context, an
exception, a log, a trace or a report. The two fields are identifiers the request itself
contains, and are kept in the failure record's diagnostics as `provider_error_param` /
`provider_error_code` (new optional fields; old records still parse). They are written **only**
on 400 records: other records serialize with `exclude_unset`, so they stay byte-identical to the
old format. The privacy-aware-trace rule (codes / counts / timings only) is unaffected; this
module emits no trace.

### 3. A refused sampling parameter is dropped and the call made again

`DEGRADABLE_SAMPLING_PARAMETERS = {"temperature", "seed"}`. A failure is a refusal of `P` when
the status is 400, `error.param == P`, `P` is in the allowlist **and** in the body that was sent,
and `error.code` is `unsupported_value` or `unsupported_parameter`. Then the client removes `P`
from the request and makes the call again — the same task, schema, messages and budget.
`seed` is included because it, too, only narrows sampling; `response_format`,
`max_completion_tokens`, `messages`, `model` and `stream` are excluded on purpose: removing them
would change the output contract or the cost. Anything else keeps the old failure, unsent again.

Bounds: each parameter is dropped at most once per call, so a call makes at most
`1 + len(allowlist) = 3` requests; a resend that is refused for a parameter that is no longer in
the body (or not in the allowlist) ends the call with the usual `provider_http_400`.

**Memory.** A process-wide registry keyed by `(chat-completions URL, model)` remembers refused
parameters (a lock guards it; `forget_unsupported_sampling_parameters()` clears it, and an
autouse fixture does so around every test). Every client of the same endpoint and model —
ingest, tree and answer, every PDF — leaves a remembered parameter out before sending, so a run
pays one refused request per parameter, not one per call. Another model or URL is probed on its
own.

### 4. Fingerprints, records and claims

- **A dropped request is fingerprinted by the body actually sent**, and is recorded, cached and
  replayed like any other call. Its `contexts/` file shows the body without the parameter.
  `omit` and an automatic drop produce the same body, so they share cache entries.
- **The refused original keeps its record** (`provider_http_400` with `provider_error_param`).
  Replaying such a record — in any process, with or without memory — is a redirect, not a
  failure: the client drops the parameter and goes on to the dropped fingerprint (a cache hit
  makes no request).
- **A call that skips a remembered parameter writes a record at its original fingerprint**
  (`failure_code="sampling_parameter_unsupported"`, `finish_category="parameter_unsupported"`,
  `http_status=null`, `provider_error_param=P`, no request made). A new process therefore
  follows every call to its cached result from disk alone, in any order, with zero requests.
  Nothing is written where a `.claim` exists without a record (another process may hold it), and
  a record already there wins.
- **An existing success** at the original fingerprint is replayed as before, even when the
  memory says the endpoint now refuses the parameter: same request, same response.
- **Records written before this ADR.** A `provider_http_400` record whose diagnostics have no
  `provider_error_param` key at all was written by a client that never read the body, so it
  cannot say why. For a body that holds an allowlisted parameter:
  - with nothing remembered yet, it is re-probed **once** with a live request recorded at
    `<fingerprint>.retry-1.json` (the existing second-attempt slot, with its own claim); the
    reply is read under the new rules and, being a refusal, redirects as above;
  - once the memory knows the parameter, a `sampling_parameter_unsupported` record is written at
    `.retry-1.json` instead, with no request;
  - in `cache_only` mode with nothing remembered, it is replayed as the old failure.
  The old record and its `.claim` are left untouched; the `.retry-1.json` record takes precedence
  from then on, so this costs one request per process at most, and none on the next rerun. A
  new-format 400 record without a parameter (the body was read and named nothing) is a terminal
  failure as before and is never re-probed.
- **A `.claim` without a record** (a kernel killed mid-call) still blocks a live call on that
  fingerprint (`request_in_progress_or_uncertain`), as before; once the memory knows a parameter,
  such a call is redirected to the dropped fingerprint without touching the claim.
  *Amended by [ADR 0023](0023-claim-takeover.md):* it blocks only while its holder may still
  run; a dead or out-of-lease holder's claim is taken over (one live call), and a claim is
  released once its record is written.

**Budget.** `live_call_count` counts transport attempts, so the refused probe and the resend
are **two** live calls against `MAX_LIVE_CALLS_PER_PDF` / the shared total. If the budget runs
out between them, the call ends `call_budget_exhausted`; the next run replays the probe's record
as a redirect and spends one call on the resend. Redirects and skip records spend nothing.

### 5. Observability

`JsonCompletionResult.dropped_parameters` and `JsonCompletionClient.dropped_parameters` name what
a call / a client went without. `FolderPipelineResult.sampling_parameters_dropped` lists what the
endpoint was found to refuse (from the registry for the configured LLM, plus the answer client),
`report.md` gets one line, and the pipeline emits a `sampling_parameters_dropped` progress event
(payload: the parameter names) before `done`. No log line prints a provider body.

### 6. Notebook `llm-selfcheck`

When the bisection's root cause is a request field in `DEGRADABLE_SAMPLING_PARAMETERS` (imported
from the implementation, not copied) and the full ingest body without it is accepted, the cell
prints that the pipeline drops it by itself (or that `OPENAI_TEMPERATURE=omit` saves the one
probe) and does **not** block Run All. Every other cause blocks as before.

## Consequences

- **Reproducibility changes on such endpoints.** A dropped temperature means the provider's
  default sampling (Azure's reasoning models: `1`), so two identical requests are no longer
  constrained to one answer by the request. ADR 0018 already found that `temperature: 0.0` pinned
  *what is sent*, not what the provider returns; the cache still guarantees that one request
  replays one response, and the dropped parameter is visible in `contexts/`, in the result and in
  the report. Endpoints that accept `temperature` keep `0.0`.
- **One refused request per process** on an endpoint that refuses the temperature (plus one per
  parameter), and zero with `OPENAI_TEMPERATURE=omit`.
- **A remembered refusal is permanent on disk**, like any failure record: if the endpoint later
  accepts the parameter for the same model and URL, those calls keep replaying the dropped
  result. A different model name gives new fingerprints; deleting `model-cache/requests/` resets
  it (and every cached answer with it).
- **The "never read a provider body" rule is narrowed**, not removed: one bounded read, on 400
  only, two checked identifiers retained, the rest discarded unread.

## Rejected alternatives

- **Always omit the temperature.** Changes every fingerprint and the sampling on endpoints that
  honour `0.0`.
- **Retry on any 400 without the parameter.** Would resend requests refused for an unrelated
  reason (a schema keyword, a missing image capability) and hide the real error.
- **Parse the error message.** Unbounded, localized, and the content this repository refuses to
  retain.
- **Delete old failure records automatically.** Destroys evidence; the `.retry-1.json` slot
  already exists for a second attempt.
- **Persist the memory in a separate file.** The skip records already carry it per call, in the
  cache directory the call uses.
