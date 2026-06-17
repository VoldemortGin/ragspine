# Documentation system — conventions

How docs are organized so an agent (or human) finds the right few hundred lines
fast, and so staleness is detectable at scale. This file is the spec; it carries
no `covers`, so it is exempt from drift tracking.

## Where docs live (find by folder first)

| Tier | Location | What | Pulled in |
|---|---|---|---|
| 0 · index | root `CLAUDE.md` | routing table only, never content | every session |
| 1 · contract | `ragspine/<domain>/CLAUDE.md` | terse per-domain invariants & gotchas | working in that subtree |
| 1 · package | `ragspine/**/__init__.py` docstring | what this level does + a `Submodules:` index of its direct children | `help()` / opening the package; enforced by `check_docstring_refs.py` |
| 1 · deep dive | `ragspine/<domain>/docs/*.md` | long reference for one subsystem | grep / explicit read |
| 2 · cross-cut | `docs/*.md`, `docs/adr/*.md` | architecture, invariants, glossary, decisions | grep / explicit read |
| — · generated | `docs/generated/` | API ref, symbol/dependency indexes — script-produced | git-ignored, never hand-edited |

Rule of thumb: **a doc lives next to the code it describes.** Only docs that
belong to no single module go in `docs/`. Co-location keeps a doc in the same
diff as the code it covers, which is what stops drift.

## Naming

- `README.md` — human-facing overview.
- `CLAUDE.md` — agent contract: operational, terse. Claude Code loads it when
  working in that directory subtree.
- `docs/*.md` — deep dives.

## Frontmatter & drift detection

Any doc that describes code carries:

```
---
covers:
  - ragspine/retrieval/rerank/
verified-against: 3c6bf0b
---
```

- `covers` — repo-relative code paths this doc describes.
- `verified-against` — the commit at which a human last confirmed doc ⇄ code agree.

`scripts/check_doc_drift.py` reports any doc whose covered **code** (Markdown is
ignored) changed since `verified-against`. After re-checking and fixing a flagged
doc, bump `verified-against` to the current HEAD:

```
.venv/bin/python scripts/check_doc_drift.py          # report all, exit 1 if any stale
.venv/bin/python scripts/check_doc_drift.py --quiet  # only stale / errored
```

That tracks **content** staleness of the curated `.md` docs. A sibling gate,
`scripts/check_docstring_refs.py` (CI step [1/3]), tracks **reference integrity**
of inline docstrings/comments: it flags dead `src/`/`docs/` links and verifies
every package's `Submodules:` index matches its real members. No `verified-against`
metadata — the references are checked directly against the tree.

### Exempt from drift (omit `covers`)

- this conventions file,
- `docs/glossary.md` — terminology, not code-bound,
- `docs/adr/*.md` — immutable historical records.

## ADRs

One decision per file: `docs/adr/NNNN-kebab-title.md`. Append-only — to reverse a
decision, add a new ADR that supersedes the old one rather than editing history.
