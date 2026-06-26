---
covers:
  - src/ragspine/service/
verified-against: 3eccc8d
---

# service — agent contract

Auto-loaded when working under `src/ragspine/service/`. Keep terse; deep dives go in
`src/ragspine/service/docs/`.

## What lives here

`ServiceConfig` (env `RAGSPINE_*`), FastAPI app (app factory + dependency
injection), RQ task queue (`FakeQueue` tests / `RQQueue` prod), ingestion jobs
(worker-owned stores), FAQ short-circuit cache, and the **Dify workflow service**
(`dify/` — L0 static gate + L1/L2 safe execution; ADR 0014): endpoints
`/v1/dify/{analyze,compile,run,run/jobs}` reuse the app factory / DI / RQ queue.

Built on the family core `corespine`: `ServiceConfig.from_env` uses `load_from_env`
(3 legacy env aliases preserved); the task queue re-exports `corespine.JobStatus`,
its `TaskQueue` Protocol extends `corespine.TaskQueue`, and `JobError` / `PathNotAllowedError`
inherit `CorespineError` with stable codes. External error shape `{type,message,stage,retryable}`
is unchanged (normalized via `error_to_dict`).

## Invariants

- **FAQ conservative exclusions** — structured-numeric / competitor / real-time /
  expired / disabled / RESTRICTED content must never short-circuit. The FAQ layer
  sits in front of the anti-fabrication guard, so a wrong short-circuit bypasses it.
- **Dify run is a trust boundary** — `/v1/dify/{analyze,compile}` never execute (always
  safe); `/v1/dify/run[/jobs]` is default-off (`dify_run_enabled=False` → 403) and, when
  on, always passes L0 static gate (warnings reject + import allowlist) → L1 restricted
  builtins sandbox (no open/os/network; `__build_class__` + guarded `__import__`) → (Linux)
  L2 subprocess + SIGKILL + setrlimit. The `provider` is server-decided; clients can never
  inject `provider_expr` (isolated process / worker rebuild it via `build_provider`).

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
