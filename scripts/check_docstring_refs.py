#!/usr/bin/env python3
"""Flag dead path/doc references inside ragspine docstrings and comments.

Module docstrings and comments are the file-level contract — the first thing a
human or coding-agent reads when opening a file. Unlike the curated ``.md`` docs
(whose *content staleness* is tracked by ``scripts/check_doc_drift.py`` via
``covers`` + ``verified-against``), inline docstrings rot a different way: they
keep pointing at paths that moved in the deep-layout reorg (``src/chunking.py``),
at numbered design docs that were folded into ADRs (``docs/02 §2``), or at
symbols that were renamed/removed. That is *reference integrity*, and unlike
semantic staleness it is directly checkable — no per-file ``verified-against``
ritual, no metadata in the prose.

This is a deterministic broken-link checker for code comments. It scans only
documentation regions (docstrings via ``ast`` + ``#`` comments via ``tokenize``),
never runtime string literals, so a real ``Path("src/...")`` in code is ignored.

What it flags (a reference that resolves to nothing in the current repo):
  - ``src/...``                      — the pre-reorg flat layout is gone.
  - ``ragspine/...`` / ``ragspine/....py`` — must exist at that path.
  - ``docs/...``                     — must exist (file or dir; ``docs/02`` also
                                       tries ``docs/02.md``).
  - ``scripts/...`` / ``tests/...`` / ``data/...`` paths — must exist.

It does NOT (yet) check backtick symbol references — prose mentions too many
names that are not meant as live links; that would be a noisier opt-in mode.

Usage (always from the repo root):

    .venv/bin/python scripts/check_docstring_refs.py          # report, exit 1 if any dead ref
    .venv/bin/python scripts/check_docstring_refs.py --quiet  # print only dead refs

Exit code is non-zero when any dead reference is found, so it can gate CI.
"""
from __future__ import annotations

import argparse
import ast
import re
import tokenize
from pathlib import Path

SCAN_ROOT = 'ragspine'  # only library code; tests/scripts docstrings are lower-stakes

# Tokens that look like an intra-repo reference. Anchored to known top dirs (plus
# the dead ``src/``) so prose like "see the agent" is not mistaken for a path.
_REF_RE = re.compile(r'(?<![\w./-])(src|ragspine|docs|scripts|tests|data)/[\w./§-]*[\w/]')

# References that LOOK like a repo path but point at EXTERNAL docs — not dead links.
# `docs/version3.x` is PaddleOCR's official documentation, cited in
# pdf_scanned_extractor as "见官方 docs/version3.x ...", not this repo's docs/ tree.
_EXTERNAL_ALLOWLIST = frozenset({'docs/version3.x'})


def find_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / '.project-root').exists():
            return candidate
    raise SystemExit('error: cannot locate .project-root (run from inside the repo)')


def doc_regions(path: Path) -> list[tuple[int, str]]:
    """Yield (lineno, text) for every docstring and comment in a .py file.

    Docstrings come from ``ast`` (module/class/function), comments from
    ``tokenize`` — deliberately excluding ordinary string literals so runtime
    values are never scanned.
    """
    source = path.read_text(encoding='utf-8')
    regions: list[tuple[int, str]] = []

    tree = ast.parse(source, filename=str(path))
    nodes = [tree, *(n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]
    for node in nodes:
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            regions.append((first.value.lineno, first.value.value))

    with path.open('rb') as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type == tokenize.COMMENT:
                regions.append((tok.start[0], tok.string))
    return regions


def resolves(ref: str, root: Path) -> bool:
    """True if the reference points at something that exists in the current repo."""
    if ref.startswith('src/'):
        return False  # the pre-reorg flat layout was removed wholesale
    candidate = ref.split('§')[0].strip().rstrip('/.,;:)')
    if not candidate:
        return False
    # Try the path as-is, then as a module ('ragspine/eval/extraction_eval' ->
    # .py) or a doc citation that dropped its suffix ('docs/architecture' -> .md).
    return any((root / f'{candidate}{suffix}').exists() for suffix in ('', '.py', '.md'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiet', action='store_true', help='print only dead refs')
    args = parser.parse_args()

    root = find_root(Path.cwd())
    scanned = dead = 0

    for path in sorted((root / SCAN_ROOT).glob('**/*.py')):
        if '__pycache__' in path.parts:
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        for lineno, text in doc_regions(path):
            for line_off, line in enumerate(text.splitlines()):
                for m in _REF_RE.finditer(line):
                    ref = m.group(0)
                    if ref in _EXTERNAL_ALLOWLIST:
                        continue  # external library docs, not a repo path
                    if resolves(ref, root):
                        continue
                    dead += 1
                    why = 'pre-reorg src/ layout removed' if ref.startswith('src/') \
                        else 'path/doc does not exist'
                    print(f'DEAD  {rel}:{lineno + line_off}  {ref}  ({why})')

    if not args.quiet or dead:
        print(f'\n{scanned} files scanned · {dead} dead references')
    return 1 if dead else 0


if __name__ == '__main__':
    raise SystemExit(main())
