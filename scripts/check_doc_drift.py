#!/usr/bin/env python3
"""Flag docs whose covered code changed since they were last verified.

A drift-tracked doc declares, in YAML frontmatter:

    ---
    covers:
      - src/ragspine/retrieval/rerank/
    verified-against: 3c6bf0b
    ---

`verified-against` is the git commit at which a human last confirmed the doc
matches the code under `covers`. This script asks git whether any non-Markdown
file under `covers` changed between that commit and HEAD; if so the doc is
potentially stale and is reported. After re-checking (and fixing) a flagged doc,
bump `verified-against` to the current HEAD.

Docs without a `covers` field are exempt by design — conventions docs,
glossaries, and ADRs (immutable history). See ``docs/README.md``.

Usage (always from the repo root):

    .venv/bin/python scripts/check_doc_drift.py          # report all, exit 1 if any stale
    .venv/bin/python scripts/check_doc_drift.py --quiet  # print only stale / errored docs

Exit code is non-zero when any doc is stale or errored, so it can gate CI.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

# Markdown is always excluded from the "did code change" check: a doc must never
# count edits to itself (or sibling docs) as drift of the code it describes.
SKIP_DIRS = {'.venv', 'venv', '.git', '__pycache__', 'node_modules', '.idea'}
SKIP_REL_PREFIXES = ('docs/generated/',)  # machine-produced, never drift-tracked


def find_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / '.project-root').exists():
            return candidate
    raise SystemExit('error: cannot locate .project-root (run from inside the repo)')


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Parse the small YAML frontmatter subset we control (str values + block lists)."""
    if not text.startswith('---'):
        return None
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return None
    meta: dict[str, object] = {}
    key: str | None = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('- ') and key is not None:
            bucket = meta.setdefault(key, [])
            if isinstance(bucket, list):
                bucket.append(stripped[2:].strip())
            continue
        if ':' in raw:
            name, _, value = raw.partition(':')
            key = name.strip()
            value = value.strip()
            meta[key] = value if value else []
    return meta


def as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def changed_code_files(verified: str, covers: list[str], root: Path) -> list[str]:
    cmd = ['git', 'diff', '--name-only', f'{verified}..HEAD', '--', *covers]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f'git diff failed against {verified!r}')
    return [f for f in proc.stdout.splitlines() if f.strip() and not f.endswith('.md')]


def iter_docs(root: Path):
    for path in sorted(root.glob('**/*.md')):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SKIP_REL_PREFIXES):
            continue
        yield path, rel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiet', action='store_true', help='print only stale / errored docs')
    args = parser.parse_args()

    root = find_root(Path.cwd())
    tracked = stale = errored = 0

    for path, rel in iter_docs(root):
        meta = parse_frontmatter(path.read_text(encoding='utf-8'))
        if not meta:
            continue
        covers = as_list(meta.get('covers'))
        if not covers:
            continue  # exempt: conventions / glossary / ADR
        tracked += 1

        verified = meta.get('verified-against')
        if not isinstance(verified, str) or not verified or verified.startswith('<'):
            stale += 1
            print(f'UNVERIFIED  {rel}  (no verified-against commit)')
            continue

        try:
            changed = changed_code_files(verified, covers, root)
        except RuntimeError as exc:
            errored += 1
            print(f'ERROR       {rel}  ({exc})')
            continue

        if changed:
            stale += 1
            print(f'STALE       {rel}  verified@{verified}')
            for changed_file in changed:
                print(f'              ↳ {changed_file}')
        elif not args.quiet:
            print(f'ok          {rel}  verified@{verified}')

    print(f'\n{tracked} tracked · {stale} stale · {errored} errored')
    return 1 if (stale or errored) else 0


if __name__ == '__main__':
    raise SystemExit(main())
