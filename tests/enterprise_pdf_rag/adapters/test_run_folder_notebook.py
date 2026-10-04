"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import json
import re
from pathlib import Path
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


def _eval_code_cells() -> dict[str, str]:
    return {
        cell["id"]: _source(cell)
        for cell in _notebook()["cells"]
        if cell["id"].startswith("eval-") and cell["cell_type"] == "code"
    }


def test_evaluation_outputs_live_under_the_project_data_dir_per_question_set() -> None:
    config = _eval_code_cells()["eval-config"]
    assert "from ragspine.common.evidence.configs import ROOT_DIR" in config
    assert re.search(r'ROOT_DIR\s*/\s*"data"\s*/\s*"eval"\s*/\s*\w+', config)
    assert "settings.questions_path" in config and ".stem" in config
    # 评测一律落盘, 不再以"是否设置 NB_REPORT_DIR"为条件, 也不用系统临时目录
    for name, source in _eval_code_cells().items():
        assert "tempfile" not in source, name
        assert "只展示" not in source and "没有落盘" not in source, name


def test_evaluation_never_writes_under_report_dir() -> None:
    cells = _eval_code_cells()
    for name, source in cells.items():
        # REPORT_DIR 只许出现在只读的 report.json 回退输入和下面的防御判断里
        for line in source.splitlines():
            if re.search(r"\bREPORT_DIR\b", line):
                assert 'REPORT_DIR / "report.json"' in line or (
                    "is_relative_to(REPORT_DIR.resolve())" in line
                ), (name, line)
    # 防御: 输出目录落在 REPORT_DIR 之内时拒绝落盘
    config = cells["eval-config"]
    assert "is_relative_to" in config and ".resolve()" in config
    assert "拒绝" in config


def test_evaluation_markdown_points_outputs_at_data_eval_not_report_dir() -> None:
    markdown = next(_source(cell) for cell in _notebook()["cells"] if cell["id"] == "eval-md")
    assert "<NB_REPORT_DIR>/eval" not in markdown
    assert "data/eval/<题集文件名>" in markdown


def _code_cells() -> list[tuple[str, str]]:
    return [
        (cell["id"], _source(cell)) for cell in _notebook()["cells"] if cell["cell_type"] == "code"
    ]


def _code_cell(cell_id: str) -> str:
    return dict(_code_cells())[cell_id]


def test_report_dir_is_fixed_under_project_data_and_never_read_from_settings() -> None:
    config = _code_cell("config")
    assert re.search(
        r'^REPORT_DIR\s*=.*ROOT_DIR\s*/\s*"data"\s*/\s*"reports"', config, re.MULTILINE
    )
    assert "QUESTIONS.stem" in config and "run-folder" in config
    # notebook 完全不读 NB_REPORT_DIR / settings.report_dir (该变量只对 CLI / 直接调用管线有效)
    for name, source in _code_cells():
        assert "settings.report_dir" not in source, name
        assert "NB_REPORT_DIR" not in source, name
    assert "report_dir=REPORT_DIR" in _code_cell("run")


def test_write_guard_cell_sits_between_config_and_the_main_run() -> None:
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("config") < ids.index("write-guard") < ids.index("run")
    guard = _code_cell("write-guard")
    for name in (
        "INGESTION_ROOT",
        "REPORT_DIR",
        "PDF_DIR",
        "is_relative_to",
        "expanduser",
        "resolve",
    ):
        assert name in guard
    assert re.search(r'ROOT_DIR\s*/\s*"data"\s*/\s*"eval"', guard)
    assert "raise" in guard


def _run_guard(root: Path, pdf_dir: Path | None, ingestion: Path, questions: Path | None) -> str:
    """把护栏 cell 抽出来执行(不联网、不碰真实仓库); 返回它打印的内容, 违规时抛异常。"""
    stem = "run-folder" if questions is None else questions.stem
    namespace: dict[str, Any] = {
        "ROOT_DIR": root,
        "PDF_DIR": pdf_dir,
        "QUESTIONS": questions,
        "INGESTION_ROOT": ingestion,
        "REPORT_DIR": root / "data" / "reports" / stem,
    }
    exec(compile(_code_cell("write-guard"), "write-guard", "exec"), namespace)
    return "ok"


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_write_guard_accepts_default_layout_and_pdf_dir_inside_data_but_disjoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    (root / "data" / "pdfs").mkdir(parents=True)
    questions = root / "data" / "questions" / "q1.jsonl"
    for pdf_dir in (tmp_path / "ro-pdfs", root / "data" / "pdfs", None):
        _run_guard(root, pdf_dir, root / "data" / "ingestion", questions)
    _run_guard(root, tmp_path / "ro-pdfs", root / "data" / "ingestion", None)


def test_write_guard_rejects_ingestion_outside_data_before_creating_anything(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "ingestion"
    before = _tree(tmp_path)
    with pytest.raises(Exception, match="APP_INGESTION_DIR") as info:
        _run_guard(root, tmp_path / "pdfs", outside, None)
    assert str(outside.resolve()) in str(info.value) and "data/" in str(info.value)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("pdf_relative", ["data", "data/ingestion", "data/reports", "data/eval"])
def test_write_guard_rejects_pdf_dir_that_contains_or_equals_a_write_dir(
    tmp_path: Path, pdf_relative: str
) -> None:
    root = tmp_path / "root"
    (root / "data" / "ingestion").mkdir(parents=True)
    pdf_dir = root / pdf_relative
    pdf_dir.mkdir(parents=True, exist_ok=True)
    before = _tree(pdf_dir)
    questions = root / "data" / "questions" / "q1.jsonl"
    with pytest.raises(Exception, match="PDF") as info:
        _run_guard(root, pdf_dir, root / "data" / "ingestion", questions)
    assert "只读" in str(info.value)
    assert _tree(pdf_dir) == before


def test_write_guard_rejects_ingestion_inside_the_pdf_dir(tmp_path: Path) -> None:
    root = tmp_path / "root"
    pdf_dir = root / "data" / "pdfs"
    pdf_dir.mkdir(parents=True)
    with pytest.raises(Exception, match="APP_INGESTION_DIR"):
        _run_guard(root, pdf_dir, pdf_dir / "ingestion", None)
    assert _tree(pdf_dir) == []


def test_evaluation_checks_its_own_dir_and_the_effective_deepeval_cache_dir() -> None:
    config = _code_cell("eval-config")
    assert "check_write_dir(" in config  # 与护栏同一套规则, 不满足时提示并跳过(不抛异常)
    judge = _code_cell("eval-judge")
    setdefault = judge.index('os.environ.setdefault("DEEPEVAL_CACHE_FOLDER"')
    effective = judge.index('os.getenv("DEEPEVAL_CACHE_FOLDER")')
    assert setdefault < effective < judge.index("from deepeval.metrics import GEval")
    assert "check_write_dir(" in judge


def test_evaluation_writes_only_sequential_whole_files_never_appends_or_seeks() -> None:
    # Databricks Unity Catalog volumes 不支持追加写与随机写(zip / xlsx 的就地写也因此失败):
    # 结果 jsonl 每题完成后整文件重写到旁边的 .partial 再 os.replace; xlsx 先写进内存再一次性落盘
    cells = _eval_code_cells()
    for name, source in cells.items():
        assert not re.search(r"""\.open\(\s*["']a""", source), name
        assert not re.search(r"""open\([^)]*["']a\+?["']""", source), name
    run = cells["eval-run"]
    assert "os.replace(" in run and ".partial" in run
    summary = cells["eval-summary"]
    assert "to_excel(EVAL_XLSX" not in summary
    assert "io.BytesIO()" in summary and "EVAL_XLSX.write_bytes(" in summary
