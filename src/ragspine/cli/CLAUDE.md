# cli — console CLI contract

Auto-loaded when working under `src/ragspine/cli/`. Keep terse.

## What lives here

The `ragspine` console command (entry point `ragspine.cli:main` in `pyproject.toml`).
A thin argparse wrapper over the zero-SDK offline core — **never** shells out to
`scripts/` (those aren't shipped in the wheel).

- `main.py` — argparse subcommand dispatch; sole public entry `main(argv)`.
  - `quickstart` — headline: fully offline, no key. Builds an ephemeral fact KB in a
    temp dir, runs `answer_question` twice with `MockProvider` — one found (with
    provenance), one absent (honest "not found" refusal). Demonstrates the two code-level
    invariants in seconds.
  - `ask` — mirrors `scripts/ask.py`: `FactStore` from `--db`, `MockProvider` by default
    (`anthropic` only with the `[llm]` extra + key, lazy-imported), prints answer + sources.
  - `version` — `importlib.metadata.version("rag-spine")`.
  - `batch <questions> --workspace DIR [--retrieval-only]` — batch ask / retrieval-only eval;
    orchestration in `batch.py` (lazy-imported: resume, `--concurrency`, `results.jsonl` +
    `summary.md` under `data/output/batch/`), pure logic in `eval/retrieval_only.py`. Retrieval-only
    goes through `RAGSpine.open_retriever()` (same guards/assembly as `ask`, no intent filters).
    Missing workspace / empty chunk store → stderr + exit 2 (db-guard `ENTRY_POINTS`). Real models
    via `--embedding/--reranker local-http --persist-vectors` (no new `RAGSPINE_*` env).
    `run_settings.json` pins the run's scoring settings; `--resume` with different settings → exit 2.
    A precheck opens the retriever once (ask's guards) and reports the actual vector channel in the summary.
  - `workflow serve <file-or-template-id> [--port N] [--open]` — starts the API + packaged
    Studio on `127.0.0.1` (fixed, no `--host`) and auto-loads the selected workflow via an
    opaque launch-session token (`/studio/?launch=<token>`; contents/paths/credentials never
    enter the URL). Needs the `[service]` extra — fastapi/uvicorn are lazy-imported, missing
    extra is an honest stderr error + exit 2. Never touches `RAGSPINE_DIFY_RUN_ENABLED`.

## Invariants

- **stdlib argparse only** — zero new deps (no Typer/Click); keep the core zero-SDK.
- **Each subcommand is a thin wrapper over package APIs** — call into `agent` / `storage`,
  never re-implement orchestration or anti-fabrication here.
- **`quickstart` stays fully offline / no-key** — `MockProvider`, ephemeral temp KB,
  cleaned up on exit. It is the no-friction proof of anti-fabrication + provenance.

## Read before editing

- `main` returns an `int` exit code; the `__main__` guard does `raise SystemExit(main())`.
  Missing-`--db` is an honest error to stderr with a non-zero return, never a silent empty answer.

## Out of scope (not yet)

- No `worker` / `demo` / `topology` subcommands — they need shipped `scripts/`, which breaks
  "self-contained on the zero-SDK offline core". (`workflow serve` *is* provided: it needs the
  `[service]` extra but keeps the core self-contained via lazy import + honest error.)
