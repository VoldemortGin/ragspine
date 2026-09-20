"""Reject infrastructure dependencies and I/O in the pure figures package."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES = (
    "enterprise_pdf_rag.figures",
    "enterprise_pdf_rag.documents",
    "enterprise_pdf_rag.processing",
)
FORBIDDEN_STDLIB = {
    "os",
    "pathlib",
    "io",
    "socket",
    "sqlite3",
    "subprocess",
    "http",
    "urllib",
    "importlib",
}


def main() -> int:
    problems: list[str] = []
    domain_paths = [
        path
        for package in PACKAGES
        for path in (ROOT / "src" / package.replace(".", "/")).rglob("*.py")
    ]
    for path in domain_paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                names = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "", node.lineno)]
            for name, line in names:
                root = name.split(".")[0]
                if any(name == package or name.startswith(package + ".") for package in PACKAGES):
                    continue
                if root not in sys.stdlib_module_names or root in FORBIDDEN_STDLIB:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{line}: forbidden domain import {name}"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"open", "eval", "exec", "__import__"}
            ):
                problems.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: forbidden domain operation {node.func.id}"
                )
    for problem in problems:
        print(problem)
    if not problems:
        print("Pure figures/documents/processing architecture verified.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
