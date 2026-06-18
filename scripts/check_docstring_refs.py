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
  - ``src/ragspine/...``             — the package lives under ``src/``; must exist.
  - a bare ``ragspine/...`` or an old flat ``src/<mod>.py`` — gone after the move
                                       under ``src/``; flagged as dead.
  - ``docs/...``                     — must exist (file or dir; ``docs/02`` also
                                       tries ``docs/02.md``).
  - ``scripts/...`` / ``tests/...`` / ``data/...`` paths — must exist.

It does NOT (yet) check backtick symbol references — prose mentions too many
names that are not meant as live links; that would be a noisier opt-in mode.

Second check: every package ``__init__.py`` must carry a ``Submodules:`` index
that matches its directory's real members exactly (subpackages as ``name/``,
modules as ``name.py``). A listed-but-absent member is dead; a real-but-unlisted
member is incomplete. This keeps the per-level "what I do + what's below me"
self-description from silently rotting as modules are added or moved.

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

SCAN_ROOT = 'src/ragspine'  # only library code; tests/scripts docstrings are lower-stakes

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
    candidate = ref.split('§')[0].strip().rstrip('/.,;:)')
    if not candidate:
        return False
    # Try the path as-is, then as a module ('src/ragspine/eval/extraction_eval' ->
    # .py) or a doc citation that dropped its suffix ('docs/architecture' -> .md).
    return any((root / f'{candidate}{suffix}').exists() for suffix in ('', '.py', '.md'))


def _actual_children(pkg_dir: Path) -> set[str]:
    """Real package members — subpackages as 'name/', modules as 'name.py'
    (excluding __init__.py, caches, dotfiles, and non-package dirs / docs)."""
    out: set[str] = set()
    for child in pkg_dir.iterdir():
        if child.name in {'__init__.py', '__pycache__'} or child.name.startswith('.'):
            continue
        if child.is_dir() and (child / '__init__.py').exists():
            out.add(f'{child.name}/')
        elif child.is_file() and child.suffix == '.py':
            out.add(child.name)
    return out


def _listed_submodules(docstring: str | None) -> set[str] | None:
    """Tokens under a package docstring's 'Submodules:' section, or None if absent."""
    if not docstring:
        return None
    lines = docstring.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == 'Submodules:')
    except StopIteration:
        return None
    out: set[str] = set()
    for ln in lines[start + 1:]:
        if not ln.strip() or not ln.startswith((' ', '\t')):
            break  # blank line or dedent ends the section
        token = re.split(r'\s*[—–-]\s*', ln.strip(), maxsplit=1)[0].strip()
        if token:
            out.add(token)
    return out


def check_package_index(root: Path) -> int:
    """Verify each package __init__.py's 'Submodules:' index matches its real
    members exactly: listed-but-absent = dead, real-but-unlisted = incomplete.
    Keeps the per-level "what I do + what's below me" self-description from
    silently rotting when modules are added or moved. Returns the issue count."""
    issues = 0
    for init in sorted((root / SCAN_ROOT).glob('**/__init__.py')):
        if '__pycache__' in init.parts:
            continue
        actual = _actual_children(init.parent)
        if not actual:
            continue  # leaf package with nothing to index
        rel = init.relative_to(root).as_posix()
        listed = _listed_submodules(
            ast.get_docstring(ast.parse(init.read_text(encoding='utf-8'))))
        if listed is None:
            issues += 1
            print(f'NO-INDEX  {rel}  (no "Submodules:" section)')
            continue
        for tok in sorted(listed - actual):
            issues += 1
            print(f'DEAD-SUB  {rel}  lists "{tok}" — not a real member')
        for tok in sorted(actual - listed):
            issues += 1
            print(f'UNLISTED  {rel}  real member "{tok}" not in Submodules')
    return issues


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
                    why = 'path/doc does not exist'
                    print(f'DEAD  {rel}:{lineno + line_off}  {ref}  ({why})')

    issues = check_package_index(root)
    if not args.quiet or dead or issues:
        print(f'\n{scanned} files scanned · {dead} dead references · '
              f'{issues} package-index issues')
    return 1 if (dead or issues) else 0


if __name__ == '__main__':
    raise SystemExit(main())
