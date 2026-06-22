---
covers:
  - src/ragspine/service/
verified-against: 18a866e
---

# service — agent contract

Auto-loaded when working under `src/ragspine/service/`. Keep terse; deep dives go in
`src/ragspine/service/docs/`.

## What lives here

`ServiceConfig` (env `RAGSPINE_*`), FastAPI app (app factory + dependency
injection), RQ task queue (`FakeQueue` tests / `RQQueue` prod), ingestion jobs
(worker-owned stores), FAQ short-circuit cache.

Built on the family core `corespine`: `ServiceConfig.from_env` uses `load_from_env`
(3 legacy env aliases preserved); the task queue re-exports `corespine.JobStatus`,
its `TaskQueue` Protocol extends `corespine.TaskQueue`, and `JobError` / `PathNotAllowedError`
inherit `CorespineError` with stable codes. External error shape `{type,message,stage,retryable}`
is unchanged (normalized via `error_to_dict`).

## Invariants

- **FAQ conservative exclusions** — structured-numeric / competitor / real-time /
  expired / disabled / RESTRICTED content must never short-circuit. The FAQ layer
  sits in front of the anti-fabrication guard, so a wrong short-circuit bypasses it.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
