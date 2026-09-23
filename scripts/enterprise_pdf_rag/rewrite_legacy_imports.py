"""Rewrite legacy ``enterprise_pdf_rag`` imports and source paths to their homes (ADR 0022).

The map is ``src/enterprise_pdf_rag/_moves.py`` — read as a literal, never imported, so this
also runs where the package itself cannot. Three passes, all by module (never by string):

- ``from enterprise_pdf_rag.<pkg> import <module>[, ...]`` → one ``from <parent> import``
  per moved module, keeping the local name;
- dotted names: the longest *module* prefix in the map is replaced and the attribute tail
  kept, which covers imports, ``monkeypatch`` strings and ``"module:attr"`` specs alike;
- ``src/enterprise_pdf_rag/<old>.py`` source paths.

Names with no mapped module prefix — the ``enterprise_pdf_rag`` JSON key, ``tests.*`` paths,
the AIA lane still pending — are left alone and listed in the residue report. With
``--only-existing`` an entry is rewritten only once its target file exists, so batches can
run it repeatedly; it is idempotent. Finish with ``ruff check --fix --select I`` and
``ruff format``.

Usage:
    python scripts/enterprise_pdf_rag/rewrite_legacy_imports.py [--only-existing] [PATH ...]
"""

import argparse
import ast
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOVES_FILE = ROOT / "src" / "enterprise_pdf_rag" / "_moves.py"
DEFAULT_PATHS = ("src", "tests", "scripts", "deploy", "Makefile")
# files that spell the legacy names on purpose
KEEP = frozenset(
    ROOT / rel
    for rel in (
        "src/enterprise_pdf_rag/_moves.py",
        "src/enterprise_pdf_rag/_shim.py",
        "tests/test_legacy_enterprise_pdf_rag.py",
        "scripts/enterprise_pdf_rag/rewrite_legacy_imports.py",
    )
)
SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "node_modules"}
)

_FROM_IMPORT = re.compile(
    r"^(?P<indent>[ \t]*)from (?P<package>enterprise_pdf_rag(?:\.\w+)*) import "
    r"(?P<names>\w+(?: as \w+)?(?:, *\w+(?: as \w+)?)*)[ \t]*$",
    re.MULTILINE,
)
_DOTTED = re.compile(r"(?<![\w.])enterprise_pdf_rag((?:\.\w+)+)")
_SOURCE_PATH = re.compile(r"\bsrc/enterprise_pdf_rag/((?:\w+/)*\w+)\.py\b")
_RESIDUE = re.compile(r"(?<![\w.])enterprise_pdf_rag\.\w+")


def load_moves(path: Path = MOVES_FILE) -> dict[str, str]:
    """Read ``MOVES`` from ``_moves.py`` without importing the package."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MOVES"
            and node.value is not None
        ):
            moves: dict[str, str] = ast.literal_eval(node.value)
            return moves
    raise ValueError(f"no MOVES literal in {path}")


def rewrite(text: str, moves: Mapping[str, str], exists: Callable[[str], bool]) -> str:
    """Return ``text`` with every mapped legacy module name and source path rewritten."""

    def target(old: str) -> str | None:
        new = moves.get(old)
        return new if new is not None and exists(new) else None

    def from_import(match: re.Match[str]) -> str:
        package = match["package"]
        lines: list[str] = []
        for clause in match["names"].split(","):
            name, _, alias = clause.strip().partition(" as ")
            new = target(f"{package}.{name}")
            if new is None:
                return match[0]
            parent, _, leaf = new.rpartition(".")
            local = alias or name
            suffix = "" if leaf == local else f" as {local}"
            lines.append(f"{match['indent']}from {parent} import {leaf}{suffix}")
        return "\n".join(lines)

    def dotted(match: re.Match[str]) -> str:
        parts = match[1].lstrip(".").split(".")
        for size in range(len(parts), 0, -1):
            new = target(".".join(["enterprise_pdf_rag", *parts[:size]]))
            if new is not None:
                return ".".join([new, *parts[size:]])
        return match[0]

    def source_path(match: re.Match[str]) -> str:
        new = target("enterprise_pdf_rag." + match[1].replace("/", "."))
        return match[0] if new is None else "src/" + new.replace(".", "/") + ".py"

    text = _FROM_IMPORT.sub(from_import, text)
    text = _DOTTED.sub(dotted, text)
    return _SOURCE_PATH.sub(source_path, text)


def _files(paths: Sequence[Path]) -> Iterator[Path]:
    for path in paths:
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        for candidate in candidates:
            if candidate.is_file() and not SKIP_DIRS.intersection(candidate.parts):
                yield candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories to rewrite")
    parser.add_argument(
        "--only-existing",
        action="store_true",
        help="rewrite an entry only once its target module exists under src/",
    )
    args = parser.parse_args(argv)
    paths: list[Path] = args.paths or [ROOT / p for p in DEFAULT_PATHS if (ROOT / p).exists()]
    moves = load_moves()

    def exists(new: str) -> bool:
        if not args.only_existing:
            return True
        base = ROOT.joinpath("src", *new.split("."))
        return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()

    changed = 0
    residue: list[str] = []
    for path in _files(paths):
        if path.resolve() in KEEP:
            continue
        try:
            before = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        after = rewrite(before, moves, exists)
        if after != before:
            path.write_bytes(after.encode("utf-8"))
            changed += 1
        residue.extend(
            f"{path}:{number}: {line.strip()}"
            for number, line in enumerate(after.splitlines(), 1)
            if _RESIDUE.search(line)
        )
    print(f"rewrote {changed} file(s); {len(residue)} residual legacy reference(s)")
    for line in residue:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
