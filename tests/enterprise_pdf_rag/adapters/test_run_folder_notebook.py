"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import json
import re
from typing import Any

import pytest

from ragspine.common.evidence.configs import ROOT_DIR

_NOTEBOOK = ROOT_DIR / "notebooks" / "run_folder.ipynb"
_SECRET_ASSIGNMENT = re.compile(r"(api_key|secret|token|password)\s*=", re.IGNORECASE)
# restart 格在 import ragspine 之前执行、拿不到 get_settings(); 只豁免这一个只读的平台探测表达式
_ALLOWED_ENV_PROBE = 'os.environ.get("DATABRICKS_RUNTIME_VERSION")'
# 评测一节在 import deepeval 之前设置的四个非密钥第三方库开关(deepeval 只认环境变量); 只放行这四个确切的键名
_DEEPEVAL_ENV_KEYS = (
    "DEEPEVAL_TELEMETRY_OPT_OUT",
    "DEEPEVAL_UPDATE_WARNING_OPT_IN",
    "DEEPEVAL_DISABLE_DOTENV",
    "DEEPEVAL_CACHE_FOLDER",
)
_ALLOWED_DEEPEVAL_WRITE = re.compile(
    r"os\.environ\.setdefault\(\"(?:" + "|".join(_DEEPEVAL_ENV_KEYS) + r")\", "
)


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
            env_checked = _ALLOWED_DEEPEVAL_WRITE.sub("", source.replace(_ALLOWED_ENV_PROBE, ""))
            assert "os.environ" not in env_checked and "environ[" not in env_checked
            assert "/Users/" not in source and "C:\\" not in source
            assert not re.search(r"""["']/[A-Za-z]""", source)


def test_environment_write_allowlist_admits_only_the_four_deepeval_switches() -> None:
    allowed = 'os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")'
    assert "os.environ" not in _ALLOWED_DEEPEVAL_WRITE.sub("", allowed)
    for other in (
        'os.environ.setdefault("OPENAI_API_KEY", "x")',
        'os.environ.setdefault("DEEPEVAL_API_KEY", "x")',
        'os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"',
        'os.environ.update({"DEEPEVAL_TELEMETRY_OPT_OUT": "1"})',
    ):
        assert "os.environ" in _ALLOWED_DEEPEVAL_WRITE.sub("", other)


def test_notebook_ends_with_an_optional_deepeval_evaluation_section() -> None:
    joined = "\n".join(_source(cell) for cell in _notebook()["cells"])
    for marker in (
        "答案评测（deepeval",
        "load_questions",
        "FIELD_MAP",
        "JUDGE_FAKE",
        "from deepeval.metrics import GEval",
        "SingleTurnParams",
        "eval_results.jsonl",
    ):
        assert marker in joined
    # deepeval 是可选的延迟 import: 只能出现在函数体内(缩进行), 不能在模块顶层 import
    assert not re.search(r"^(?:from|import) deepeval", joined, re.MULTILINE)
    # 四个开关必须先于 deepeval 的 import 出现
    first_import = joined.index("from deepeval.metrics import GEval")
    assert all(joined.index(key) < first_import for key in _DEEPEVAL_ENV_KEYS)
