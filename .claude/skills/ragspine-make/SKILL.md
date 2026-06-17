---
name: ragspine-make
description: "How to run dev / CI-CD tasks in the RAGSpine repo via the Makefile. Use whenever you need to install deps, run tests, run the local CI gate, lint/format, check doc drift, run the demo, ask a question, run evals, start the server/worker, or regenerate fixtures in this project. Keywords: make, Makefile, run tests, pytest, CI, ci gate, pre-push, lint, ruff, mypy, format, doc drift, demo, ask, eval, qa eval, retrieval ab, serve, FastAPI, worker, RQ, redis, fixtures, install, venv, uv, hooks, build docs, 跑测试, 跑CI, 本地CI, 格式化, 漂移, 评估, 启动服务, 安装依赖, 怎么运行, 命令"
allowed-tools: ["Bash", "Read"]
---

# RAGSpine — dev / CI-CD commands (Makefile)

The repo wraps common commands in a root `Makefile`. **Always run from the repo root**
(scripts anchor on `.project-root`). `make help` lists every target. The raw commands
live in `scripts/`; the Makefile is a thin, discoverable wrapper over them.

**Interpreter:** every target defaults to `.venv/bin/python`. Override with
`make <target> PYTHON=python3.12` (passed through to `scripts/ci.sh` / `lint.sh` too).

## First-time setup

| Command | What it does | Notes |
|---|---|---|
| `make venv` | `uv venv .venv` | Creates the project venv. |
| `make install` | editable install, `[dev,service]` extras | The usual dev setup. |
| `make install-all` | editable install, `[dev,service,llm,embed]` | Adds real LLM + embedding backends (heavier). `[pdf]`/`[ocr]` are platform-specific and intentionally excluded. |
| `make hooks` | `git config core.hooksPath .githooks` | **One-time per clone.** Enables the pre-push CI gate so red code never leaves the machine. Emergency bypass: `git push --no-verify`. |

## The quality gate

| Command | What it does | Notes |
|---|---|---|
| `make ci` | tests (`-m "not gpu"`) + demo smoke | **The gate** — exactly what the pre-push hook runs (`scripts/ci.sh`). This is the single source of truth for "is it green". |
| `make test` | `pytest tests/ -q -m "not gpu"` | Expect **943 passed, 1 gpu-skipped**. |
| `make test-all` | full suite incl. `gpu` marker | Needs Ubuntu + NVIDIA GPU + real OCR model; skipped/failing elsewhere. |
| `make lint` | ruff check + ruff format --check + mypy | **Informational, non-blocking** (always exits 0). The inherited codebase predates linting; adopt fixes incrementally — it is *not* wired into the CI gate yet. |
| `make fmt` | `ruff check --fix` + `ruff format` | Auto-fix lint + format `ragspine scripts tests`. |
| `make drift` | `scripts/check_doc_drift.py` | Flags docs whose covered code changed since `verified-against`. Expect **10 tracked, 0 stale**. See `docs/README.md` for the convention. |

GitHub Actions is dormant (manual-trigger only) to avoid consuming minutes — the local
gate is authoritative.

## Demo / ask / eval

| Command | What it does | Notes |
|---|---|---|
| `make demo` | `scripts/run_demo.py` | Offline end-to-end; expect `ALL CHECKS PASSED`. |
| `make ask Q="…"` | offline question via MockProvider | e.g. `make ask Q="中国内地FY2024的REVENUE是多少"`. Uses `--provider mock --db data/fact_metric.db`. Ask for missing data → honest refusal, never a guess. |
| `make eval-qa` | `scripts/run_qa_eval.py` | QA harness, **baseline-gated** (fails on regression below the frozen floor; don't launder a regression with `--update-baseline`). |
| `make eval-retrieval` | `scripts/eval_retrieval_ab.py` | BM25-vs-hybrid Recall@k / MRR. Default gold is synthetic (proves the harness math, not real recall). |

## Service

| Command | What it does | Notes |
|---|---|---|
| `make serve` | `scripts/run_server.py` | FastAPI app. |
| `make worker` | `scripts/run_worker.py` | RQ worker — **needs Redis running**. Tests use `FakeQueue`, so this is only for real async runs. |

## Fixtures / docs / housekeeping

| Command | What it does | Notes |
|---|---|---|
| `make fixtures` | regenerate synthetic demo data | Deterministic, regenerable (`make_synthetic_deck` + `make_fixtures_{excel,pptx,pdf}`). **Never add real-world data.** |
| `make docs` | `scripts/build_docs.py` | Static doc site (md→HTML). |
| `make clean` | remove `__pycache__` + `.pytest_cache` / `.ruff_cache` / `.mypy_cache` | |

## Typical loops

- **Before pushing:** `make ci` (or just push — the pre-push hook runs it).
- **TDD inner loop:** `make test` (fast, offline, deterministic).
- **Touched docs/code contracts:** `make drift`.
- **Cleaning up style:** `make fmt` then `make lint`.
