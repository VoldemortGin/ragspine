# rag-spine has been renamed to ragspine

As of 0.17.3 the RAGSpine distribution is published on PyPI as
[`ragspine`](https://pypi.org/project/ragspine/). This `rag-spine` package contains no code: it
only depends on `ragspine` at the same version, so existing `pip install rag-spine` setups keep
working. The import name (`import ragspine`) and the `ragspine` CLI are unchanged.

Please switch your dependency to `ragspine` (extras included, e.g. `ragspine[service]`; this
transitional package does not forward extras):

```bash
pip uninstall rag-spine
pip install ragspine
```

## Maintainers: publishing this package

Published by hand, after `ragspine==<version>` is live on PyPI. Bump `version` and the
`ragspine==` pin in `pyproject.toml` together. From the repository root:

```bash
python -m build --sdist --wheel --outdir dist/rag-spine-transition packaging/rag-spine-transition
python -m twine check dist/rag-spine-transition/*
python -m twine upload dist/rag-spine-transition/*   # PyPI API token scoped to the rag-spine project
```
