# PRD — Deployment Packaging: one-click self-host via Docker Compose

> **status:** implemented (Compose v1: offline-default stack + opt-in backend profiles; **Helm chart shipped** under `deploy/helm/ragspine/`) · **created:** 2026-06-19 · **methodology:** static validation (no docker/helm on the dev host — real boot-test runs on a remote docker / kind host)
> Lands under `deploy/`. Operator-facing quickstart is [`deploy/README.md`](../deploy/README.md);
> this PRD is the originating spec and carries no `covers:` frontmatter (it describes ops config,
> not library code). Built **ADDITIVELY** — only new files under `deploy/` + this PRD — so it never
> conflicts with the concurrent VectorStore (A–D) work on tracked files.

## Problem statement

RAGSpine already ships everything a backend RAG service needs — a FastAPI app, an RQ
ingestion worker, a Redis-backed queue, and a clean `RAGSPINE_*` environment surface on
`ServiceConfig` (`src/ragspine/service/config.py`). What it **lacks** is a turnkey way to
*stand the whole thing up*. Today a new operator must, by hand: create a venv, install the
right extras, run Redis, export a dozen env vars, point the stores at writable paths, and
(optionally) provision a Postgres/pgvector or Qdrant backend. The framework-free promise —
*"assemble it in plain Python"* — stops at the process boundary: there is no **one command**
that yields a running, sensible-by-default deployment.

Two failure modes make this sharper than "add a Dockerfile":

- The server entrypoint defaults to `--host 127.0.0.1` (correct for a laptop, **unreachable**
  inside a container) — a naive `docker run` boots a server nobody can reach.
- The interesting backends (pgvector, Qdrant) are real infrastructure. Forcing every operator
  to provision Postgres just to *try* RAGSpine would betray the offline-lean-core stance
  (ADR 0005 / 0009): the default must run with **zero external services and zero API keys**.

## Solution

A thin, **env-driven** `deploy/` package — no new config system, pure `RAGSPINE_*` injection
over the existing `ServiceConfig`:

- **One image, two roles.** A multi-stage, uv-based `deploy/Dockerfile` installs
  `ragspine[service,vector]` into an isolated venv; `app` (server) and `worker` share the
  **same** image and differ only by `command`. The server command hard-codes `--host 0.0.0.0`
  so it is reachable in-container. The image carries the runtime files the scripts need
  (`.project-root`, `scripts/`, `src/`, `config/`, the version-controlled `data/golden/`), with
  `WORKDIR` at the `.project-root` dir so `rootutils.setup_root` resolves.
- **Default = offline lean stack.** `docker compose -f deploy/compose.yaml up` boots
  `app + worker + redis` with `RAGSPINE_PROVIDER=mock`, `RAGSPINE_VECTOR_STORE=sqlite_vec`,
  `RAGSPINE_EMBEDDING=none` — **no API key, no external DB**. A named volume
  (`ragspine-data` → `/var/lib/ragspine`) persists the sqlite stores + uploads across restarts.
- **Real backends are opt-in via Compose profiles.** `--profile postgres` adds a
  `pgvector/pgvector` service (the app connects over the network via `RAGSPINE_PG_URL`, which
  defaults to the bundled Postgres); `--profile qdrant` adds a `qdrant/qdrant` service. Each is
  one env flip (`RAGSPINE_VECTOR_STORE=pgvector|qdrant`) — nothing else changes.
- **Secrets via `env_file`.** API keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) come from
  `deploy/.env` (gitignored; `deploy/.env.example` documents every variable). Config knobs use
  `${VAR:-default}` interpolation so the offline default needs **no** `.env` at all.

## The principle: default-offline + opt-in profiles

The deployment must *demonstrate* RAGSpine's core stance, not dilute it. So the **default path
has no secrets and no servers**: a reviewer runs one command and gets a working API that proves
anti-fabrication (honest "not found" on an empty store) in seconds. Everything heavier —
a real LLM, a networked vector DB — is a **deliberate, single-flag opt-in** (a profile + one
`RAGSPINE_*`), never a precondition. This mirrors the in-process `MockProvider` /
`InProcessVectorStore` defaults at the *library* layer: the deployment is the same pluggability
seam, expressed in Compose profiles instead of `Protocol`s. No new config system is introduced —
the profiles only gate which *services* start and which *existing* `RAGSPINE_*` values are set.

## Monorepo `deploy/` rationale (why it never ships to PyPI)

`deploy/` lives in the repo for **operators**, but the published wheel is scoped to the library:
`[tool.hatch.build.targets.wheel] packages = ["src/ragspine"]` (`pyproject.toml`) packs **only**
`src/ragspine`. So `deploy/` (Dockerfile, compose, env templates) is version-controlled next to
the code it deploys — one source of truth, no drift between "the engine" and "how you run it" —
yet `pip install rag-spine` stays lean and never drags in ops scaffolding. This is the same
"docs/config live beside code, but the artifact is minimal" discipline the repo already applies
to `docs/`, `config/`, and `scripts/`.

## Honest boundaries

- **No docker on the dev host.** This was validated **statically**: `compose.yaml` parses under
  PyYAML; each injected `RAGSPINE_*` was cross-checked against its real consumer — the 16
  `ServiceConfig.from_env` knobs against `config.py`, plus the two **by-design** non-`ServiceConfig`
  vars (`RAGSPINE_PG_URL`, read directly by the pgvector adapter via `os.environ`; `RAGSPINE_PG_PASSWORD`,
  a compose-only interpolation var feeding the bundled Postgres container's `POSTGRES_PASSWORD`) —
  so there are no dangling/bogus knobs; the server command was confirmed to carry `--host 0.0.0.0`.
  A real `docker compose up` boot-test is run **separately on a remote docker host**. `deploy/README.md`
  states this plainly.
- **Qdrant profile is forward-looking.** `make_vector_store("qdrant")` is invoked by
  `ServiceConfig` with **no kwargs**, so it runs Qdrant's **local in-process mode** (`:memory:`),
  not against a server URL — `ServiceConfig` has no Qdrant-URL env yet. The `qdrant` service is
  therefore provisioned but **not network-connected by the current wiring**; it is staged for the
  "thread a Qdrant URL through `ServiceConfig`" follow-up (which touches `config.py`, outside this
  additive deploy slice). **pgvector is the fully end-to-end external vector store today** (it
  *does* read `RAGSPINE_PG_URL`). Documented honestly in compose comments + README rather than
  papered over.
- **Empty-string env trap.** Setting a `RAGSPINE_*` var to `""` overrides the code default
  (`env.get` treats present-but-empty as a value) and some fields reject it (e.g.
  `RAGSPINE_REFERENCE_DATE=""` → `date.fromisoformat("")` raises). So optional vars stay
  **commented** in `.env.example`, and only concrete non-empty values land in compose `environment`.

## Out of scope (this increment)

- **Helm / Kubernetes** — the explicit **next step** (see below).
- **Editing any tracked file** (README, CLAUDE.md, the PRDs, `config.py`) — kept strictly additive
  to avoid conflicts with the concurrent VectorStore work; anything needing a `config.py` change
  (e.g. a networked Qdrant URL) is deferred.
- **TLS / reverse proxy / multi-replica scaling / secrets manager** — single-host Compose first;
  these belong to the Helm step or a hardening PRD.
- **Baking demo fact data into the image** — the image carries `data/golden/` (version-controlled
  eval sets); runtime stores are seeded on the named volume via ingestion, not pre-baked, so the
  image stays immutable and data stays in the volume.

## Next step: Helm

A Kubernetes **Helm chart** is the explicit follow-up (a later, separate increment — **not** done
here). It reuses the *same* image and the *same* `RAGSPINE_*` contract this PRD pins down: the
Compose `app` / `worker` / `redis` services map to Deployments, the named volume to a PVC, the
backend profiles to `values.yaml` toggles (`vectorStore: sqlite_vec | pgvector | qdrant`), and the
`env_file` secrets to a `Secret`. Landing Compose first makes the env surface and the
offline-default contract concrete, so the chart is a translation rather than a redesign.

### Shipped: Helm chart (`deploy/helm/ragspine/`)

**Status: done** — the translation above is now a real chart under
[`deploy/helm/ragspine/`](../deploy/helm/ragspine/) (quickstart:
[`deploy/helm/README.md`](../deploy/helm/README.md)), built **ADDITIVELY** (only new files
under `deploy/helm/` plus this note + a Helm pointer in `deploy/README.md`). It maps the Compose
design 1:1: `app`/`worker` → two Deployments sharing **one** image (`ragspine:local`, server
command hard-codes `--host 0.0.0.0`); the named volume → a shared data PVC at `/var/lib/ragspine`;
the profiles → `values.yaml` toggles (`vectorStore`, `postgres.enabled`, `qdrant.enabled`,
`redis.enabled` for an in-cluster Redis Deployment+Service+PVC); the `env_file` secrets → a
`Secret` rendered **only when an API key is set**; non-secret knobs → a ConfigMap consumed via
`envFrom`. The default `helm install` is the **offline lean stack** (mock + sqlite_vec, no key).
The Qdrant honesty boundary carries over verbatim: the `qdrant` Deployment is provisioned but the
current `make_vector_store("qdrant")` runs in-process `:memory:` (no networked URL through
`ServiceConfig` yet) — pgvector is the fully end-to-end external store. Validated **statically**
(no helm/kubectl/docker on the dev host): `Chart.yaml`/`values.yaml` parse under PyYAML, the
server template carries `--host 0.0.0.0`, every `RAGSPINE_*` referenced in templates exists in
`ServiceConfig.from_env` (except the adapter-read `RAGSPINE_PG_URL`). On a remote docker host,
`helm lint` passed (0 failures) and `helm template` rendered correctly in **both** the offline-default
and the `postgres`/`pgvector` profile. A live `helm install` was **not** completed: that box's kind
control-plane fails to come up (kubeadm `wait-control-plane` timeout; kubelet↔apiserver `EOF`) — a
host-infrastructure issue unrelated to the chart. The chart's app behavior is covered by the
**equivalent Docker Compose stack, live-tested end-to-end on the same host** (build + `/healthz` +
anti-fabrication refusal + the pgvector profile's isolation invariant).
