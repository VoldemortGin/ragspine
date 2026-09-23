"""Keep ``enterprise_pdf_rag.*`` imports working after the package dissolves (ADR 0022).

A meta path finder at ``sys.meta_path[0]`` answers only for names in ``_moves.py``:

- a moved module (its ``ragspine`` target is on disk) imports as an alias: the legacy name is
  bound to the *same* module object, with a ``DeprecationWarning`` attributed to the importer;
- a legacy package whose directory is gone becomes a virtual package (``__path__ == []``) that
  lazily exposes its moved children as attributes (PEP 562);
- anything not yet moved falls through to the regular import system, untouched.
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings
from collections.abc import Mapping, Set
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import CodeType, ModuleType

from enterprise_pdf_rag._moves import MOVES, PACKAGES

_PREFIX = "enterprise_pdf_rag."
# ragspine and enterprise_pdf_rag always ship side by side (one wheel, one src/)
_SRC = Path(__file__).resolve().parent.parent
# attribute the warning to the importing line, not to this file or the import machinery
_SKIP_FILES = (
    str(Path(__file__).resolve()),
    str(Path(importlib.__file__).resolve().parent),
    "<frozen importlib",
)


def _on_disk(module: str, src: Path) -> bool:
    base = src.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self._target = target
        self._target_spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        warnings.warn(
            f"{spec.name} is deprecated; import {self._target} instead (ADR 0022)",
            DeprecationWarning,
            skip_file_prefixes=_SKIP_FILES,
        )
        module = importlib.import_module(self._target)
        self._target_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # importlib stamps the legacy spec onto whatever create_module returned; hand the
        # canonical module its own spec back so reload/pickle/runpy still see the real name
        module.__spec__ = self._target_spec

    def get_code(self, fullname: str) -> CodeType | None:
        # `python -m <legacy name>` runs the canonical module's code as __main__
        spec = importlib.util.find_spec(self._target)
        loader = None if spec is None else spec.loader
        if not isinstance(loader, importlib.abc.InspectLoader):
            raise ImportError(f"cannot load code for {self._target}", name=fullname)
        return loader.get_code(self._target)


class _VirtualPackageLoader(importlib.abc.Loader):
    def __init__(self, children: Set[str]) -> None:
        self._children = children

    def create_module(self, spec: ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        name = module.__name__
        children = self._children

        def __getattr__(attribute: str) -> ModuleType:
            if attribute in children:
                return importlib.import_module(f"{name}.{attribute}")
            raise AttributeError(f"module {name!r} has no attribute {attribute!r}")

        def __dir__() -> list[str]:
            return sorted(children)

        vars(module).update(__getattr__=__getattr__, __dir__=__dir__)


class LegacyFinder(importlib.abc.MetaPathFinder):
    """Alias moved legacy modules; leave everything else to the regular finders."""

    def __init__(
        self,
        moves: Mapping[str, str] = MOVES,
        packages: Set[str] = PACKAGES,
        src: Path = _SRC,
    ) -> None:
        self._moves = moves
        self._packages = packages
        self._src = src

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> ModuleSpec | None:
        if not fullname.startswith(_PREFIX):
            return None
        if fullname in self._packages:
            if _on_disk(fullname, self._src):
                return None
            prefix = fullname + "."
            children = {
                child.removeprefix(prefix)
                for child in (*self._moves, *self._packages)
                if child.startswith(prefix) and "." not in child.removeprefix(prefix)
            }
            return ModuleSpec(fullname, _VirtualPackageLoader(children), is_package=True)
        new = self._moves.get(fullname)
        if new is None or not _on_disk(new, self._src):
            return None
        return ModuleSpec(fullname, _AliasLoader(new))


def install() -> None:
    """Put one ``LegacyFinder`` in front of the regular finders (idempotent)."""
    if not any(isinstance(finder, LegacyFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, LegacyFinder())
