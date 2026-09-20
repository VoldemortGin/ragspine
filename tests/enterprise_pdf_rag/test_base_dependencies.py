"""The base install must satisfy every third-party import of ``enterprise_pdf_rag``.

``pip install rag-spine`` (no extras) ships the ``enterprise-pdf-rag`` console script, so each
absolute third-party import in the package must resolve to a base ``[project].dependencies``
entry, directly or through one of its unconditional requirements.
"""

import ast
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "enterprise_pdf_rag"
FIRST_PARTY = {"enterprise_pdf_rag", "ragspine"}


def _third_party_imports() -> set[str]:
    names: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                names.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.partition(".")[0])
    return names - set(sys.stdlib_module_names) - FIRST_PARTY


def _base_dependencies() -> set[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return {canonicalize_name(Requirement(spec).name) for spec in project["dependencies"]}


def _unconditional_closure(distributions: set[str]) -> set[str]:
    closure: set[str] = set()
    pending = list(distributions)
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        try:
            specs = requires(name) or []
        except PackageNotFoundError:
            continue
        for spec in specs:
            requirement = Requirement(spec)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(canonicalize_name(requirement.name))
    return closure


def test_base_dependencies_cover_every_enterprise_pdf_rag_import() -> None:
    available = _unconditional_closure(_base_dependencies())
    module_distributions = packages_distributions()
    missing = {
        module
        for module in _third_party_imports()
        if not any(
            canonicalize_name(distribution) in available
            for distribution in module_distributions.get(module, [module])
        )
    }
    assert missing == set(), (
        f"imported by enterprise_pdf_rag but absent from base dependencies: {sorted(missing)}"
    )
