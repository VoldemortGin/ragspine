"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import json
import re
from typing import Any

import pytest

from ragspine.common.evidence.configs import ROOT_DIR

_NOTEBOOK = ROOT_DIR / "notebooks" / "run_folder.ipynb"
_SECRET_ASSIGNMENT = re.compile(r"(api_key|secret|token|password)\s*=", re.IGNORECASE)


def _notebook() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_NOTEBOOK.read_text(encoding="utf-8"))
    return loaded


def _source(cell: dict[str, Any]) -> str:
    return "".join(cell["source"])


def test_notebook_is_valid_nbformat_4_with_a_generic_kernel() -> None:
    notebook = _notebook()
    assert notebook["nbformat"] == 4
    assert notebook["metadata"]["kernelspec"]["name"] == "python3"
    ids = [cell["id"] for cell in notebook["cells"]]
    assert len(set(ids)) == len(ids)
    for cell in notebook["cells"]:
        assert cell["cell_type"] in {"markdown", "code"}
        assert isinstance(cell["source"], list) and isinstance(cell["metadata"], dict)
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


def test_notebook_code_compiles_and_reads_configuration_from_settings() -> None:
    code = [_source(cell) for cell in _notebook()["cells"] if cell["cell_type"] == "code"]
    for index, source in enumerate(code):
        # IPython magic lines (%pip install ...) are not Python syntax; drop them before compiling
        python_only = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("%")
        )
        compile(python_only, f"cell-{index}", "exec")
    joined = "\n".join(code)
    assert "from ragspine.common.evidence.configs import get_settings" in joined
    assert "run_folder_pipeline(" in joined and "make_answer_llm()" in joined
    for name in ("MAX_LIVE_CALLS_PER_PDF", "MAX_LIVE_CALLS_TOTAL", "BUILD_TREE", "REQUALIFY"):
        assert name in joined
    assert "chart_member_count" in joined


@pytest.mark.parametrize("cell_type", ["code", "markdown"])
def test_notebook_has_no_secrets_environment_writes_or_absolute_paths(cell_type: str) -> None:
    for cell in _notebook()["cells"]:
        if cell["cell_type"] != cell_type:
            continue
        source = _source(cell)
        assert "sk-" not in source
        assert not _SECRET_ASSIGNMENT.search(source)
        if cell_type == "code":
            assert "os.environ" not in source and "environ[" not in source
            assert "/Users/" not in source and "C:\\" not in source
            assert not re.search(r"""["']/[A-Za-z]""", source)
