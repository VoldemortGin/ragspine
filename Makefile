# RAGSpine — common dev / CI-CD commands.
#
# `make` or `make help` lists every target. Targets honour the project venv by default;
# override the interpreter with:  make test PYTHON=python3.12
#
# Always run from the repo root (the scripts anchor on .project-root).

.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
VENV   ?= .venv

# ---- setup ---------------------------------------------------------------------------

.PHONY: venv
venv: ## Create the project virtualenv (.venv) with uv
	uv venv $(VENV)

.PHONY: install
install: ## Editable install with dev+service+vector extras (the usual dev setup)
	VIRTUAL_ENV="$(CURDIR)/$(VENV)" uv pip install -e ".[dev,service,vector]"

.PHONY: install-all
install-all: ## Editable install with all non-GPU extras (dev,service,vector,llm,embed)
	VIRTUAL_ENV="$(CURDIR)/$(VENV)" uv pip install -e ".[dev,service,vector,llm,embed]"

.PHONY: hooks
hooks: ## Enable the pre-push CI gate (one-time, per clone)
	git config core.hooksPath .githooks
	@echo "pre-push CI gate enabled (emergency bypass: git push --no-verify)"

# ---- quality gate --------------------------------------------------------------------

.PHONY: ci
ci: ## Local CI gate: tests + demo smoke (exactly what pre-push runs)
	PYTHON=$(PYTHON) scripts/ci.sh

.PHONY: test
test: ## Run the test suite (excludes gpu- and network-marked tests)
	$(PYTHON) -m pytest tests/ -q -m "not gpu and not network"

.PHONY: test-all
test-all: ## Run the full suite including gpu-marked tests
	$(PYTHON) -m pytest tests/ -q

.PHONY: lint
lint: ## ruff + mypy (informational, non-blocking)
	PYTHON=$(PYTHON) scripts/lint.sh

.PHONY: fmt
fmt: ## Auto-fix lint and format with ruff
	$(PYTHON) -m ruff check --fix src/ragspine scripts tests
	$(PYTHON) -m ruff format src/ragspine scripts tests

.PHONY: drift
drift: ## Flag docs whose covered code changed since last verified
	$(PYTHON) scripts/check_doc_drift.py

# ---- demo / eval ---------------------------------------------------------------------

.PHONY: demo
demo: ## Run the offline end-to-end demo (expects ALL CHECKS PASSED)
	$(PYTHON) scripts/run_demo.py

.PHONY: ask
ask: ## Ask offline, e.g. make ask Q="中国内地FY2024的REVENUE是多少"
	$(PYTHON) scripts/ask.py --provider mock --db data/fact_metric.db "$(Q)"

.PHONY: eval-qa
eval-qa: ## QA evaluation against the golden set (baseline-gated)
	$(PYTHON) scripts/run_qa_eval.py

.PHONY: eval-retrieval
eval-retrieval: ## BM25-vs-hybrid retrieval A/B harness
	$(PYTHON) scripts/eval_retrieval_ab.py

# ---- service -------------------------------------------------------------------------

.PHONY: serve
serve: ## Run the FastAPI server
	$(PYTHON) scripts/run_server.py

.PHONY: worker
worker: ## Run the RQ worker (needs Redis)
	$(PYTHON) scripts/run_worker.py

# ---- studio (Web UI) -----------------------------------------------------------------

.PHONY: studio-install
studio-install: ## Install Studio frontend dependencies (pnpm, node 22)
	cd studio && pnpm install

.PHONY: studio-dev
studio-dev: ## Run the Studio Vite dev server (proxies API to :8000)
	cd studio && pnpm dev

.PHONY: studio-build
studio-build: ## Build the Studio static bundle into src/ragspine/service/studio_dist
	cd studio && pnpm build

# ---- fixtures / docs -----------------------------------------------------------------

.PHONY: fixtures
fixtures: ## Regenerate the synthetic demo fixtures (deterministic)
	$(PYTHON) scripts/make_synthetic_deck.py
	$(PYTHON) scripts/make_fixtures_excel.py
	$(PYTHON) scripts/make_fixtures_pptx.py
	$(PYTHON) scripts/make_fixtures_pdf.py

.PHONY: docs
docs: ## Build the static API-reference site from docstrings (pdoc → docs/site, nginx-ready)
	@# Enumerate leaf modules explicitly. Lazy package __init__ modules are navigation
	@# indexes, not API definitions; passing both them and leaves makes pdoc add every
	@# module twice and emits hundreds of misleading duplicate-module warnings.
	find src/ragspine -name '*.py' ! -name '__init__.py' | sed 's|^src/||; s|\.py$$||; s|/|.|g' \
		| sort -u | tr '\n' '\0' | xargs -0 $(PYTHON) -m pdoc -o docs/site
	@echo "API docs → docs/site/  (deploy: point nginx 'root' at $(abspath docs/site))"

.PHONY: clean
clean: ## Remove caches (pyc, pytest/ruff/mypy) and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

# ---- release -------------------------------------------------------------------------

.PHONY: build
build: ## Build wheel + sdist into dist/ and validate with twine check
	rm -rf dist
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

.PHONY: publish-test
publish-test: build ## Build, then upload to TestPyPI (rehearse a real release; needs a TestPyPI token)
	$(PYTHON) -m twine upload --repository testpypi dist/*

.PHONY: publish
publish: build ## Build, then upload to PyPI (real release; needs a PyPI token)
	$(PYTHON) -m twine upload dist/*

# ---- meta ----------------------------------------------------------------------------

.PHONY: help
help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
