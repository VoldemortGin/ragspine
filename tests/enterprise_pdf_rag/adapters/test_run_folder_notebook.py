"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import contextlib
import csv
import errno
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from ragspine.common.evidence.configs import ROOT_DIR

_NOTEBOOK = ROOT_DIR / "notebooks" / "run_folder.ipynb"
_SECRET_ASSIGNMENT = re.compile(r"(api_key|secret|token|password)\s*=", re.IGNORECASE)
# restart 格在 import ragspine 之前执行、拿不到 get_settings(); 只豁免这一个只读的平台探测表达式
_ALLOWED_ENV_PROBE = 'os.environ.get("DATABRICKS_RUNTIME_VERSION")'


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
            env_checked = source.replace(_ALLOWED_ENV_PROBE, "")
            assert "os.environ" not in env_checked and "environ[" not in env_checked
            assert "/Users/" not in source and "C:\\" not in source
            assert not re.search(r"""["']/[A-Za-z]""", source)


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


@pytest.mark.parametrize("pdf_relative", ["data", "data/ingestion", "data/reports"])
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


def test_diagnostic_cells_sit_before_the_run_and_the_selfcheck_follows_the_guard() -> None:
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("diagnose") < ids.index("version-check") < ids.index("config")
    assert ids.index("write-guard") < ids.index("fs-selfcheck") < ids.index("run")
    version = _code_cell("version-check")
    for marker in ("git", "file_placement", "link_new_file", "report.json", "platform.platform()"):
        assert marker in version


def test_selfcheck_writes_only_under_a_cleaned_up_ingestion_subdir_never_the_pdf_dir() -> None:
    source = _code_cell("fs-selfcheck")
    assert re.search(r'INGESTION_ROOT\)\.expanduser\(\)\s*/\s*f"_selfcheck_', source)
    assert "shutil.rmtree(" in source and "link_new_file" in source
    for line in source.splitlines():
        if "PDF_DIR" in line:
            assert not re.search(r"write|mkdir|open\(|replace|link|rmtree|unlink", line), line


_SELFCHECK_PDF = b"%PDF-1.4 synthetic"


def _conclusion(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("结论"))


def _run_selfcheck(root: Path, with_pdf: bool = True) -> tuple[str, list[str], list[str]]:
    """抽出 fs-selfcheck cell 在 tmp 根下执行; 返回 (输出, PDF 目录树, ingestion 目录树)。"""
    pdf_dir = root / "pdfs"
    pdf_dir.mkdir(parents=True)
    if with_pdf:
        (pdf_dir / "a.pdf").write_bytes(_SELFCHECK_PDF)
    ingestion = root / "data" / "ingestion"
    namespace: dict[str, Any] = {"PDF_DIR": pdf_dir, "INGESTION_ROOT": ingestion}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("fs-selfcheck"), "fs-selfcheck", "exec"), namespace)
    if with_pdf:
        assert (pdf_dir / "a.pdf").read_bytes() == _SELFCHECK_PDF
    return buffer.getvalue(), _tree(pdf_dir), _tree(ingestion)


def test_selfcheck_runs_clean_and_removes_its_temporary_directory(tmp_path: Path) -> None:
    output, pdf_tree, ingestion_tree = _run_selfcheck(tmp_path)
    assert pdf_tree == ["a.pdf"]
    assert ingestion_tree == []
    assert "[失败]" not in output and "全部可用" in _conclusion(output)
    assert "link_new_file 放置新文件" in output and "[清理] 已删除" in output


def test_selfcheck_survives_hard_links_being_refused_and_still_concludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "link", refuse)
    output, pdf_tree, ingestion_tree = _run_selfcheck(tmp_path)
    assert pdf_tree == ["a.pdf"] and ingestion_tree == []
    assert re.search(r"\[失败\] os\.link 硬链接.*\n\s+PermissionError", output)
    assert f"errno={errno.EPERM}" in output
    assert "硬链接不可用" in _conclusion(output)
    assert "入库应能正常进行" in _conclusion(output)
    # 生产函数走回退: 放置新文件成功, 对已存在目标仍抛 FileExistsError
    assert "[成功] link_new_file 放置新文件" in output
    assert "[成功] link_new_file 目标已存在应抛 FileExistsError" in output


def test_selfcheck_reports_replace_failure_and_skips_cleanly_without_pdfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(os, "link", refuse)
    output, _, ingestion_tree = _run_selfcheck(tmp_path, with_pdf=False)
    assert ingestion_tree == []
    assert "没有可读的 PDF" in output
    assert "os.replace 也不可用" in _conclusion(output)


def test_the_evaluation_section_is_gone() -> None:
    joined = "\n".join(_source(cell) for cell in _notebook()["cells"]).casefold()
    assert "deepeval" not in joined and "data/eval" not in joined
    assert not [cell["id"] for cell in _notebook()["cells"] if cell["id"].startswith("eval-")]


def test_answers_cell_follows_the_run_and_writes_three_columns_in_one_whole_file() -> None:
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("run") < ids.index("answers")
    source = _code_cell("answers")
    assert 'REPORT_DIR / "answers.csv"' in source
    assert '["question", "expected", "answer"]' in source
    assert "utf-8-sig" in source and "os.replace(" in source and "csv.writer(" in source
    assert not re.search(r"""open\([^)]*["']a\+?["']""", source)


class _Case:
    def __init__(self, question: str, expected: str | None, answer: str | None) -> None:
        self.case_id = question[:8]
        self.question = question
        self.expected = expected
        self.answer = answer


class _Eval:
    def __init__(self, cases: list[_Case]) -> None:
        self.cases = cases


class _Result:
    def __init__(self, eval_summary: _Eval | None) -> None:
        self.eval = eval_summary


def _run_answers(report_dir: Path, result: _Result) -> str:
    namespace: dict[str, Any] = {"result": result, "REPORT_DIR": report_dir}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("answers"), "answers", "exec"), namespace)
    return buffer.getvalue()


def test_answers_csv_round_trips_awkward_text_with_a_bom_and_one_row_per_question(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports" / "set"
    rows = [
        ("收入, 是多少?", "18.1亿", '答: "18.1亿"\n第二行'),
        ("no answer", None, None),
        ("multi\nline question", "a,b", ""),
        ("收入, 是多少?", "18.1亿", "duplicate question is kept"),
    ]
    cases = [_Case(*row) for row in rows]
    # A first, longer run must be replaced whole, never appended to.
    report_dir.mkdir(parents=True)
    (report_dir / "answers.csv").write_text("stale," * 500, encoding="utf-8")

    _run_answers(report_dir, _Result(_Eval(cases)))

    raw = (report_dir / "answers.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"stale" not in raw
    with (report_dir / "answers.csv").open(encoding="utf-8-sig", newline="") as handle:
        table = list(csv.reader(handle))
    assert table[0] == ["question", "expected", "answer"]
    assert table[1:] == [[q, e or "", a or ""] for q, e, a in rows]
    assert sorted(path.name for path in report_dir.iterdir()) == ["answers.csv"]


def test_answers_cell_skips_without_a_question_set_and_never_raises(tmp_path: Path) -> None:
    for empty in (_Result(None), _Result(_Eval([]))):
        output = _run_answers(tmp_path / "reports", empty)
        assert "不生成 answers.csv" in output
    assert not (tmp_path / "reports").exists()
