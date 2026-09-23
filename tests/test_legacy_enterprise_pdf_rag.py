"""The legacy ``enterprise_pdf_rag.*`` import paths keep resolving while the package dissolves.

ADR 0022 moves every module into a ``ragspine.<domain>.evidence`` subtree. ``_moves.py`` names
each target once; ``_shim.py`` aliases the legacy name to the *same* module object, so class
identity, ``isinstance`` and ``monkeypatch`` by dotted string all keep working. Until a module
is moved its legacy file is still the real module and no alias (and no warning) exists — the
parametrized cases assert whichever side is true, never skip, so every batch keeps the count.
"""

import importlib
import importlib.util
import subprocess
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from enterprise_pdf_rag._moves import MOVES, PACKAGES, PENDING
from enterprise_pdf_rag._shim import LegacyFinder

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CODEMOD = ROOT / "scripts" / "enterprise_pdf_rag" / "rewrite_legacy_imports.py"
# a real canonical module stands in for a moved one, so the alias path is exercised before
# the first batch moves anything
PROBE_TARGET = "ragspine.common.sensitivity"
PROBE_OLD = "enterprise_pdf_rag._legacy_probe"
PROBE_PACKAGE = "enterprise_pdf_rag._legacy_probe_pkg"


def _on_disk(module: str) -> bool:
    base = SRC.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _shim_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in caught if "ADR 0022" in str(w.message)]


@pytest.fixture
def probe_finder(monkeypatch: pytest.MonkeyPatch) -> Iterator[LegacyFinder]:
    finder = LegacyFinder(
        moves={
            PROBE_OLD: PROBE_TARGET,
            f"{PROBE_PACKAGE}.inner": PROBE_TARGET,
        },
        packages=frozenset({PROBE_PACKAGE}),
    )
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    yield finder
    for name in [n for n in sys.modules if n.startswith((PROBE_OLD, PROBE_PACKAGE))]:
        del sys.modules[name]
    package = sys.modules["enterprise_pdf_rag"]
    for attribute in ("_legacy_probe", "_legacy_probe_pkg"):
        if hasattr(package, attribute):
            delattr(package, attribute)


# ---- the frozen map ------------------------------------------------------------------------


def test_every_legacy_module_is_either_mapped_or_pending() -> None:
    assert len(MOVES) + len(PENDING) == 144
    assert not set(MOVES) & PENDING
    assert len(PACKAGES) == 8


def test_targets_are_distinct_canonical_modules() -> None:
    targets = list(MOVES.values())
    assert len(set(targets)) == len(targets)
    assert all(t.startswith("ragspine.") for t in targets)
    assert all(".evidence" in t for t in targets)


def test_every_legacy_module_lives_in_a_legacy_package() -> None:
    parents = {name.rsplit(".", 1)[0] for name in (*MOVES, *PENDING)}
    assert parents <= PACKAGES | {"enterprise_pdf_rag"}


# ---- each real entry: aliased once moved, untouched before --------------------------------


@pytest.mark.parametrize(("old", "new"), sorted(MOVES.items()))
def test_legacy_module_resolves(old: str, new: str) -> None:
    if _on_disk(new):
        sys.modules.pop(old, None)
        with pytest.warns(DeprecationWarning, match="ADR 0022"):
            legacy = importlib.import_module(old)
        assert legacy is importlib.import_module(new)
    else:
        assert _on_disk(old), f"{old} is neither at its legacy path nor at {new}"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            legacy = importlib.import_module(old)
        assert _shim_warnings(caught) == []
        assert legacy.__name__ == old


@pytest.mark.parametrize("old", sorted(PENDING))
def test_pending_module_stays_at_its_legacy_path(old: str) -> None:
    assert _on_disk(old)
    assert importlib.import_module(old).__name__ == old


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_legacy_package_resolves(package: str) -> None:
    module = importlib.import_module(package)
    children = sorted(
        name for name in MOVES if name.rsplit(".", 1)[0] == package and _on_disk(MOVES[name])
    )
    if _on_disk(package):
        assert module.__file__ is not None
    else:
        assert list(module.__path__) == []
    for child in children:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            attribute = getattr(module, child.rsplit(".", 1)[1])
        assert attribute is importlib.import_module(MOVES[child])


# ---- alias mechanics, on a synthetic entry ------------------------------------------------


@pytest.mark.usefixtures("probe_finder")
def test_alias_is_the_canonical_module_and_warns_at_the_importer() -> None:
    with pytest.warns(DeprecationWarning, match=f"{PROBE_OLD} is deprecated") as record:
        legacy = importlib.import_module(PROBE_OLD)
    assert legacy is importlib.import_module(PROBE_TARGET)
    assert sys.modules[PROBE_OLD] is sys.modules[PROBE_TARGET]
    assert [w.filename for w in record] == [__file__]


@pytest.mark.usefixtures("probe_finder")
def test_alias_leaves_the_canonical_spec_intact() -> None:
    with pytest.warns(DeprecationWarning):
        legacy = importlib.import_module(PROBE_OLD)
    assert legacy.__name__ == PROBE_TARGET
    assert legacy.__spec__ is not None
    assert legacy.__spec__.name == PROBE_TARGET


@pytest.mark.usefixtures("probe_finder")
def test_alias_keeps_class_identity() -> None:
    from ragspine.common.sensitivity import SensitivityPolicy

    with pytest.warns(DeprecationWarning):
        legacy = importlib.import_module(PROBE_OLD)
    assert legacy.SensitivityPolicy is SensitivityPolicy
    assert isinstance(SensitivityPolicy(), legacy.SensitivityPolicy)


@pytest.mark.usefixtures("probe_finder")
def test_monkeypatch_by_legacy_string_patches_the_canonical_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.warns(DeprecationWarning):
        importlib.import_module(PROBE_OLD)
    sentinel = object()
    monkeypatch.setattr(f"{PROBE_OLD}.classify_sensitivity", sentinel)
    assert importlib.import_module(PROBE_TARGET).classify_sensitivity is sentinel


@pytest.mark.usefixtures("probe_finder")
def test_virtual_package_lazily_exposes_moved_children() -> None:
    package = importlib.import_module(PROBE_PACKAGE)
    assert list(package.__path__) == []
    assert "inner" in dir(package)
    with pytest.warns(DeprecationWarning):
        inner = package.inner
    assert inner is importlib.import_module(PROBE_TARGET)
    with pytest.raises(AttributeError):
        _ = package.missing
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"{PROBE_PACKAGE}.missing")


def test_finder_ignores_everything_else() -> None:
    finder = LegacyFinder()
    assert finder.find_spec("json", None) is None
    assert finder.find_spec("ragspine.common.sensitivity", None) is None
    assert finder.find_spec("enterprise_pdf_rag.not_a_module", None) is None


def test_finder_is_installed_exactly_once() -> None:
    import enterprise_pdf_rag._shim as shim

    shim.install()
    assert sum(isinstance(f, LegacyFinder) for f in sys.meta_path) == 1


def test_run_module_executes_an_aliased_module_as_main() -> None:
    script = (
        "import runpy, sys\n"
        "from enterprise_pdf_rag._shim import LegacyFinder\n"
        f"sys.meta_path.insert(0, LegacyFinder(moves={{{PROBE_OLD!r}: 'ragspine.cli.main'}},"
        " packages=frozenset()))\n"
        "sys.argv = ['probe', '--help']\n"
        f"runpy.run_module({PROBE_OLD!r}, run_name='__main__', alter_sys=True)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout


def test_python_m_legacy_cli_still_runs() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "enterprise_pdf_rag.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout


# ---- the import codemod ---------------------------------------------------------------------


def _codemod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rewrite_legacy_imports", CODEMOD)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CODEMOD_MOVES = {
    "enterprise_pdf_rag.adapters.json_completion": (
        "ragspine.common.evidence.providers.json_completion"
    ),
    "enterprise_pdf_rag.adapters.chart_qa": "ragspine.extraction.evidence.adapters.chart_qa.chart_qa",
    "enterprise_pdf_rag.adapters.chart_qa_displayed": (
        "ragspine.extraction.evidence.adapters.chart_qa.chart_qa_displayed"
    ),
    "enterprise_pdf_rag.adapters.source_paint": (
        "ragspine.extraction.evidence.adapters.source_paint.source_paint"
    ),
    "enterprise_pdf_rag.processing.retrieval": "ragspine.retrieval.evidence.index.snapshot",
    "enterprise_pdf_rag.cli": "ragspine.cli.evidence",
}


def _rewrite(text: str, moved: frozenset[str] | None = None) -> str:
    def exists(new: str) -> bool:
        return moved is None or new in moved

    rewritten: str = _codemod().rewrite(text, CODEMOD_MOVES, exists)
    return rewritten


def test_codemod_reads_the_same_map_as_the_shim() -> None:
    assert _codemod().load_moves() == MOVES


def test_codemod_rewrites_dotted_names_by_longest_module_prefix() -> None:
    before = (
        "from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver\n"
        "import enterprise_pdf_rag.adapters.json_completion as jc\n"
        'monkeypatch.setattr("enterprise_pdf_rag.adapters.json_completion._send_once", f)\n'
        'uvicorn.run("enterprise_pdf_rag.cli:main")\n'
    )
    assert _rewrite(before) == (
        "from ragspine.extraction.evidence.adapters.chart_qa.chart_qa_displayed"
        " import StoredDisplayResolver\n"
        "import ragspine.common.evidence.providers.json_completion as jc\n"
        'monkeypatch.setattr("ragspine.common.evidence.providers.json_completion._send_once", f)\n'
        'uvicorn.run("ragspine.cli.evidence:main")\n'
    )


def test_codemod_rewrites_submodule_imports_from_a_legacy_package() -> None:
    before = (
        "from enterprise_pdf_rag.adapters import source_paint\n"
        "    from enterprise_pdf_rag.processing import retrieval\n"
        "from enterprise_pdf_rag import cli\n"
        "from enterprise_pdf_rag.adapters import chart_qa as cq, json_completion\n"
    )
    assert _rewrite(before) == (
        "from ragspine.extraction.evidence.adapters.source_paint import source_paint\n"
        "    from ragspine.retrieval.evidence.index import snapshot as retrieval\n"
        "from ragspine.cli import evidence as cli\n"
        "from ragspine.extraction.evidence.adapters.chart_qa import chart_qa as cq\n"
        "from ragspine.common.evidence.providers import json_completion\n"
    )


def test_codemod_leaves_wire_formats_and_unmapped_names_alone() -> None:
    before = (
        '{"enterprise_pdf_rag": {"status": "abstained"}}\n'
        "enterprise_pdf_rag.status=abstained\n"
        "from tests.enterprise_pdf_rag.adapters.chart_qa import helper\n"
        "from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar\n"
        "from enterprise_pdf_rag.adapters import aia_ingestion, source_paint\n"
        "model id enterprise-pdf-rag/0123456789ab\n"
    )
    assert _rewrite(before) == before


def test_codemod_only_existing_skips_targets_not_yet_moved() -> None:
    before = (
        "from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver\n"
        "from enterprise_pdf_rag.cli import main\n"
    )
    assert _rewrite(before, moved=frozenset({"ragspine.cli.evidence"})) == (
        "from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver\n"
        "from ragspine.cli.evidence import main\n"
    )


def test_codemod_rewrites_source_paths() -> None:
    before = "see src/enterprise_pdf_rag/processing/retrieval.py and src/enterprise_pdf_rag/x.py\n"
    assert _rewrite(before) == (
        "see src/ragspine/retrieval/evidence/index/snapshot.py and src/enterprise_pdf_rag/x.py\n"
    )


def test_codemod_is_idempotent() -> None:
    before = "from enterprise_pdf_rag.adapters import source_paint\nimport enterprise_pdf_rag.cli\n"
    once = _rewrite(before)
    assert _rewrite(once) == once


def test_codemod_main_rewrites_files_in_place(tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "user.py"
    target.parent.mkdir()
    target.write_text("from enterprise_pdf_rag.processing.retrieval import X\n", encoding="utf-8")
    untouched = tmp_path / "blob.bin"
    untouched.write_bytes(b"\xff\xfeenterprise_pdf_rag.processing.retrieval")
    assert _codemod().main([str(tmp_path)]) == 0
    assert target.read_text(encoding="utf-8") == (
        "from ragspine.retrieval.evidence.index.snapshot import X\n"
    )
    assert untouched.read_bytes() == b"\xff\xfeenterprise_pdf_rag.processing.retrieval"
