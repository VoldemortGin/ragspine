# RAGSpine Studio

Self-hosted web console for RAGSpine: a visual drag-and-drop Dify-workflow editor
(analyze / compile / run against the RAGSpine service), a RAG playground, a read-only
pipeline topology view, and ingestion job management.

Stack: Vite + React 19 + TypeScript (strict) + React Flow (`@xyflow/react`) + Tailwind CSS v4.

## Development

Requires Node 22+ and pnpm.

```bash
cd studio
pnpm install
pnpm dev          # http://localhost:5173/studio/
```

The dev server proxies `/v1`, `/healthz`, `/readyz` to `http://localhost:8000`
(see `vite.config.ts`), so start the RAGSpine FastAPI service first:

```bash
# from the repo root
make serve        # or: .venv/bin/python scripts/run_server.py
```

To exercise workflow execution from the editor, the server must be started with
`RAGSPINE_DIFY_RUN_ENABLED=true`.

## Build

```bash
pnpm build        # typechecks (tsc --noEmit), then emits studio/dist
```

The bundle is built with `base: '/studio/'` and is meant to be mounted by the
FastAPI service under the `/studio` path. All API calls use relative URLs, so
production is same-origin with no CORS configuration.

## Tests

```bash
pnpm test         # vitest
pnpm typecheck    # tsc --noEmit
```

The test suite round-trips every backend fixture in `../tests/dify/fixtures/`
through the YAML <-> canvas conversion layer and asserts lossless preservation
(unknown fields included).

## Layout

```
src/api/        typed fetch client + response types for the RAGSpine API
src/workflow/   pure canvas model: yaml <-> graph conversion, node registry,
                dagre auto-layout, React Flow adapters (React-free, unit-tested)
src/components/ shared dark-theme UI primitives
src/pages/      Workflows (editor), Playground, Pipeline, Jobs
tests/          vitest suites for the workflow model layer
```

Workflows are persisted in browser `localStorage` (the backend has no workflow
storage); use Export YAML to keep a durable copy.
