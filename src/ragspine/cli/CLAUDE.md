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

- No `serve` / `worker` / `demo` / `topology` subcommands — they need the `[service]` extra
  or shipped `scripts/`, which breaks "self-contained on the zero-SDK offline core".
