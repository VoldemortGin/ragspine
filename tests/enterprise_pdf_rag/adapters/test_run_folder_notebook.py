"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import ast
import contextlib
import csv
import errno
import io
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from enterprise_pdf_rag.adapters import pdfspine_tsr
from enterprise_pdf_rag.adapters.answer_audit import AnswerAuditStore
from enterprise_pdf_rag.adapters.folder_pipeline import (
    DocumentRun,
    EvalCase,
    EvalSummary,
    FolderPipelineResult,
    LiveCalls,
)
from enterprise_pdf_rag.adapters.onnx_partition import ONNX_LAYOUT_MODEL_FILE, ONNX_MODELS_ENV
from ragspine.common.evidence import configs
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


def test_only_the_first_ten_questions_are_answered_by_default_and_the_run_cell_passes_it() -> None:
    assert re.search(r"^MAX_QUESTIONS\s*=\s*10\b", _code_cell("config"), re.MULTILINE)
    assert "max_questions=MAX_QUESTIONS" in _code_cell("run")


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


@pytest.mark.parametrize(
    ("mode", "probe_ok", "expected"),
    [
        ("files", True, "将使用的对象库后端: files"),
        ("auto", True, "将使用的对象库后端: sqlite"),
        ("auto", False, "探测失败码 sqlite_wal"),
        ("staged", True, "将使用的对象库后端: staged"),
    ],
)
def test_selfcheck_prints_the_object_backend_the_run_will_use_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, probe_ok: bool, expected: str
) -> None:
    """ADR 0046: auto 静默退到文件布局在 FUSE 上会写出上万个文件; 自检格把将用的后端与原因打印出来。"""
    from ragspine.common.evidence.object_backend import probe

    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", mode)
    monkeypatch.setenv("APP_OBJECT_STORE_STAGING_DIR", str(tmp_path / "staging"))
    configs.get_settings.cache_clear()
    if not probe_ok:
        monkeypatch.setattr(
            probe, "probe_directory", lambda *_a, **_k: probe.ProbeResult(False, "sqlite_wal")
        )
    try:
        output, _, ingestion_tree = _run_selfcheck(tmp_path)
    finally:
        configs.get_settings.cache_clear()
    assert expected in output
    assert ingestion_tree == []


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


# ───────────── llm-selfcheck: 入库 / tree / 回答各形态的请求体二分诊断 ─────────────

_FAKE_KEY = "unit-test-fake-key-0123"
_REPLY_OK = (200, '{"id":"x","choices":[]}')
_CONSTRAINT_KEYS = ("minLength", "maxLength", "pattern", "minItems", "maxItems")


def test_llm_selfcheck_cell_sits_between_the_fs_selfcheck_and_the_run_and_has_a_config_switch() -> (
    None
):
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("fs-selfcheck") < ids.index("llm-selfcheck") < ids.index("run")
    assert ids.index("config") < ids.index("write-guard") < ids.index("llm-selfcheck")
    assert re.search(r"^LLM_SELFCHECK\s*=\s*True\b", _code_cell("config"), re.MULTILINE)
    source = _code_cell("llm-selfcheck")
    assert "LLM_SELFCHECK" in source and "raise" in source
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "llm-selfcheck" in intro and "LLM_SELFCHECK" in intro


def test_llm_selfcheck_cell_never_prints_the_key_or_authorization_and_writes_no_environment() -> (
    None
):
    source = _code_cell("llm-selfcheck")
    for line in source.splitlines():
        if re.search(r"\bprint\(", line):
            assert not re.search(r"api_key|secret|_auth|Authorization", line, re.IGNORECASE), line
    assert "os.environ" not in source and "environ[" not in source
    assert "TemporaryDirectory(" in source and "sender=" in source


_Rule = Callable[[dict[str, Any]], tuple[int, str] | tuple[int, str, dict[str, str]]]


class _Net:
    """http.client.HTTPSConnection 的替身: 不开 socket, 记录请求, 按 rule(body) 脚本化回复。"""

    def __init__(self, rule: _Rule, error: BaseException | None = None) -> None:
        self.rule = rule
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.targets: list[tuple[str, str]] = []
        self.authorizations: list[str] = []
        self.sleeps: list[float] = []

    def connection(self) -> type:
        net = self

        class _Response:
            def __init__(self, status: int, text: str, headers: dict[str, str]) -> None:
                self.status = status
                self._text = text.encode()
                self._headers = {name.lower(): value for name, value in headers.items()}

            def getheader(self, name: str, default: str | None = None) -> str | None:
                return self._headers.get(name.lower(), default)

            def read(self, amount: int = -1) -> bytes:
                return self._text[:amount] if amount >= 0 else self._text

        class _Connection:
            def __init__(self, netloc: str, timeout: float | None = None) -> None:
                assert timeout == 30.0
                self._netloc = netloc
                self._body: dict[str, Any] = {}

            def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
                assert method == "POST"
                if net.error is not None:
                    raise net.error
                self._body = json.loads(body)
                net.sent.append(self._body)
                net.targets.append((self._netloc, path))
                net.authorizations.append(headers.get("Authorization", ""))

            def getresponse(self) -> _Response:
                status, text, *headers = net.rule(self._body)
                return _Response(status, text, headers[0] if headers else {})

            def close(self) -> None:
                pass

        return _Connection


def _schema_text(body: dict[str, Any]) -> str:
    return json.dumps(body.get("response_format", {}).get("json_schema", {}).get("schema", {}))


def _reject(message: str = "Invalid request") -> tuple[int, str]:
    return 400, json.dumps({"error": {"message": message}})


def _selfcheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rule: _Rule = lambda body: _REPLY_OK,
    *,
    switch: bool = True,
    error: BaseException | None = None,
    configured: bool = True,
) -> tuple[str, BaseException | None, _Net]:
    """抽出 llm-selfcheck cell 执行: 假网络、假配置、系统临时目录与 cwd 都指向 tmp_path。"""
    import http.client
    import tempfile

    from ragspine.common.evidence.configs import get_settings

    if configured:
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.test/v1")
        monkeypatch.setenv("OPENAI_MODEL", "model-under-test")
    get_settings.cache_clear()
    net = _Net(rule, error)
    monkeypatch.setattr(http.client, "HTTPSConnection", net.connection())
    monkeypatch.setattr("time.sleep", net.sleeps.append)  # 退避不真等
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.chdir(tmp_path)
    before = _tree(tmp_path)
    namespace: dict[str, Any] = {"LLM_SELFCHECK": switch}
    buffer = io.StringIO()
    raised: BaseException | None = None
    with contextlib.redirect_stdout(buffer):
        try:
            exec(compile(_code_cell("llm-selfcheck"), "llm-selfcheck", "exec"), namespace)
        except Exception as exc:  # noqa: BLE001 - 阻断异常是被测行为
            raised = exc
    output = buffer.getvalue()
    assert _tree(tmp_path) == before, "自检不得在磁盘上留下任何文件"
    assert _FAKE_KEY not in output and "Bearer" not in output
    assert raised is None or _FAKE_KEY not in str(raised)
    assert all(auth == f"Bearer {_FAKE_KEY}" for auth in net.authorizations)
    return output, raised, net


def _line(output: str, prefix: str) -> str:
    return next(line for line in output.splitlines() if line.strip().startswith(prefix))


def test_llm_selfcheck_all_forms_accepted_sends_four_baselines_and_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path)
    assert raised is None
    assert len(net.sent) == 4 <= 5
    assert output.count("[通过]") == 4 and "[被拒" not in output
    assert set(net.targets) == {("llm.example.test", "/v1/chat/completions")}
    assert "https://llm.example.test/v1/chat/completions" in output
    text, vision, tree, answer = net.sent
    for body in (text, vision, tree, answer):
        assert body["model"] == "model-under-test" and body["stream"] is False
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["temperature"] == 0.0 and "max_completion_tokens" in body
    assert "image_url" not in json.dumps(text) and "image_url" in json.dumps(vision)
    assert "seed" not in text and "seed" not in vision and "seed" not in tree
    assert answer["seed"] == 0
    assert "结论" in output and "不是请求体问题" in _conclusion(output)


def test_llm_selfcheck_rejected_response_format_points_at_response_format_and_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        if "response_format" in body:
            return _reject(f"response_format json_schema is not supported ({_FAKE_KEY})")
        return _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert (
        isinstance(raised, RuntimeError)
        and "入库" in str(raised)
        and "nothing_to_index" in str(raised)
    )
    assert "[被拒 HTTP 400]" in output and "is not supported" in output
    assert "response_format" in _line(output, "最先被拒的一项")
    assert "约束关键字" not in _line(output, "最先被拒的一项")
    assert "不带 response_format" in _line(output, "最接近原样且仍被接受的形态")
    assert "response_format json_schema is not supported" in _line(output, "服务端原话")
    assert len(output.split("最先被拒的一项")) == 2  # 只列一项根因
    # 全部四种形态各自都有一行基线结果, 没有被二分吞掉
    for label in ("入库 text", "入库 vision", "tree", "回答"):
        assert label in output


@pytest.mark.parametrize(
    ("keyword", "klass"),
    [
        ("maxLength", "minLength/maxLength"),
        ("pattern", "pattern"),
        ("maxItems", "minItems/maxItems"),
    ],
)
def test_llm_selfcheck_narrows_a_rejected_schema_keyword_to_exactly_that_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keyword: str, klass: str
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        if f'"{keyword}"' in _schema_text(body):
            return _reject(f"'{keyword}' is not permitted")
        return _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert isinstance(raised, RuntimeError)
    culprit = _line(output, "最先被拒的一项")
    assert klass in culprit
    assert all(other not in culprit for other in ("temperature", "image_url", "strict"))
    assert f"'{keyword}' is not permitted" in _line(output, "服务端原话")
    assert len(output.split("最先被拒的一项")) == 2
    # 其余被拒的形态只做一次"同样去掉该类"的验证, 不再各自展开二分
    assert output.count("[被拒") < 20


def test_llm_selfcheck_a_rejected_temperature_is_named_and_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR 0021: the pipeline drops a refused temperature and resends, so Run All goes on.
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        return _reject("temperature unsupported") if "temperature" in body else _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert raised is None
    assert "temperature" in _line(output, "最先被拒的一项")
    assert "response_format" not in _line(output, "最先被拒的一项")
    conclusion = _conclusion(output)
    assert "端点不接受 temperature" in conclusion and "自动去掉" in conclusion
    assert "OPENAI_TEMPERATURE=omit" in conclusion and "不阻断" in conclusion
    assert re.search(r"\[通过\] 入库 text.*原样请求只去掉 temperature", output)


def test_llm_selfcheck_uses_the_pipelines_own_allowlist() -> None:
    source = _code_cell("llm-selfcheck")
    assert "DEGRADABLE_SAMPLING_PARAMETERS" in source
    assert "json_completion import" in source


def test_llm_selfcheck_still_blocks_when_dropping_temperature_is_not_enough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        if "temperature" in body:
            return _reject("temperature unsupported")
        if "response_format" in body and "max_completion_tokens" in body:
            return _reject("this combination is refused")
        return _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert isinstance(raised, RuntimeError) and "入库" in str(raised)
    assert "temperature" in _line(output, "最先被拒的一项")
    assert "自动去掉" not in _conclusion(output)


def test_llm_selfcheck_vision_only_rejection_names_the_image_and_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        return _reject("image_url not supported") if "image_url" in json.dumps(body) else _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert isinstance(raised, RuntimeError) and "入库" in str(raised)
    assert "image_url" in _line(output, "最先被拒的一项")
    assert re.search(r"\[通过\] 入库 text", output) and re.search(
        r"\[被拒 HTTP 400\] 入库 vision", output
    )
    assert re.search(r"\[通过\] tree", output) and re.search(r"\[通过\] 回答", output)


def test_llm_selfcheck_seed_only_rejection_warns_but_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        return _reject("seed unsupported") if "seed" in body else _REPLY_OK

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert raised is None
    assert re.search(r"\[被拒 HTTP 400\] 回答", output)
    assert "seed" in _line(output, "最先被拒的一项")
    assert "不阻断" in _conclusion(output)


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError("refused"), TimeoutError("timed out"), OSError("dns failure")]
)
def test_llm_selfcheck_connection_failures_are_classified_as_network_and_not_bisected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path, error=error)
    assert isinstance(raised, RuntimeError)
    assert net.sent == []
    assert "网络 / 代理 / 证书问题\uff0c不是请求体问题" in output
    # ADR 0035: 连接错误是临时错误, 逐形态各试 1 次 (共用 2 次重试), 四种全失败才阻断
    assert output.count("[连接失败]") == 4 and output.count("重试") >= 2
    assert len(net.sleeps) == 2 and "共发出 6 次请求" in output
    assert "最先被拒的一项" not in output


def test_llm_selfcheck_auth_failure_is_not_a_body_problem_and_is_not_bisected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, raised, net = _selfcheck(
        monkeypatch, tmp_path, lambda body: (401, '{"error":"invalid api key"}')
    )
    assert isinstance(raised, RuntimeError)
    assert len(net.sent) == 1
    assert "[被拒 HTTP 401]" in output and "不是请求体问题" in _conclusion(output)


def _transient_then_ok(status: int, failures: int, headers: dict[str, str] | None = None) -> _Rule:
    left = [failures]

    def rule(body: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
        if left[0] > 0:
            left[0] -= 1
            return status, '{"error":"slow down"}', headers or {}
        return (*_REPLY_OK, {})

    return rule


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
def test_llm_selfcheck_a_transient_status_once_is_retried_and_passes_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path, _transient_then_ok(status, 1))
    assert raised is None
    assert len(net.sent) == 5 and len(net.sleeps) == 1
    assert output.count("[通过]") == 4 and "重试 1/2" in output
    assert "不是请求体问题" in _conclusion(output)
    assert "最先被拒的一项" not in output


def test_llm_selfcheck_persistent_429_warns_in_chinese_and_does_not_block_unless_all_four_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 前 3 个请求 (含 2 次重试) 全是 429, 之后端点恢复: 只有一种形态未能确认
    output, raised, net = _selfcheck(monkeypatch, tmp_path, _transient_then_ok(429, 3))
    assert raised is None
    assert len(net.sleeps) == 2 and output.count("[通过]") == 3
    assert "[被拒 HTTP 429]" in output and "未能确认" in _conclusion(output)
    assert "端点当前限流 / 不可用\uff08HTTP 429\uff09" in output
    assert "管线运行时会自动退避重试\uff0c不会把它当作永久失败" in output
    assert "请检查配额" in output and "不阻断" in _conclusion(output)


def test_llm_selfcheck_blocks_only_when_all_four_forms_fail_transiently_and_caps_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, raised, net = _selfcheck(
        monkeypatch, tmp_path, lambda body: (503, '{"error":"overloaded"}')
    )
    assert isinstance(raised, RuntimeError) and "临时错误" in str(raised)
    assert "不会写入失败缓存" in str(raised) and "永久缓存" not in str(raised)
    assert len(net.sent) == 6 and len(net.sleeps) == 2  # 4 基线 + 共用的 2 次重试, 不二分
    assert "共发出 6 次请求" in output and "端点当前整体不可用" in _conclusion(output)
    assert "最先被拒的一项" not in output


def test_llm_selfcheck_honours_retry_after_capped_at_thirty_seconds_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ragspine.common.evidence.providers.transient import RETRY_BASE_DELAY, RETRY_MAX_DELAY

    _, raised, net = _selfcheck(
        monkeypatch, tmp_path, _transient_then_ok(429, 1, {"Retry-After": "7"})
    )
    assert raised is None and len(net.sleeps) == 1
    assert 7 <= net.sleeps[0] <= 7 + RETRY_BASE_DELAY
    (tmp_path / "b").mkdir()
    _, raised, net = _selfcheck(
        monkeypatch, tmp_path / "b", _transient_then_ok(503, 1, {"Retry-After": "600"})
    )
    assert raised is None and len(net.sleeps) == 1
    assert RETRY_MAX_DELAY <= net.sleeps[0] <= RETRY_MAX_DELAY + RETRY_BASE_DELAY
    (tmp_path / "c").mkdir()
    _, _, net = _selfcheck(
        monkeypatch, tmp_path / "c", _transient_then_ok(429, 1)
    )  # 无 Retry-After
    assert 0 < net.sleeps[0] <= 2 * RETRY_BASE_DELAY


def test_llm_selfcheck_classification_comes_from_the_implementation_not_a_copy() -> None:
    source = _code_cell("llm-selfcheck")
    assert "providers.transient import" in source and "transient_status" in source
    assert "retry_after_seconds" in source
    numbers = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant)}
    assert not numbers & {408, 429, 500, 502, 503, 504}


@pytest.mark.parametrize("status", [401, 403, 404, 407])
def test_llm_selfcheck_permanent_statuses_still_block_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path, lambda body: (status, "{}"))
    assert isinstance(raised, RuntimeError) and net.sleeps == []
    assert len(net.sent) == 1 and f"[被拒 HTTP {status}]" in output
    assert "不是请求体问题" in _conclusion(output) and "永久缓存" not in str(raised)


def test_llm_selfcheck_switch_off_sends_nothing_and_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path, switch=False)
    assert raised is None and net.sent == [] and "跳过" in output


def test_llm_selfcheck_without_llm_configuration_skips_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, raised, net = _selfcheck(monkeypatch, tmp_path, configured=False)
    assert raised is None and net.sent == [] and "跳过" in output


def test_llm_selfcheck_own_bug_prints_and_never_blocks_the_main_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def rule(body: dict[str, Any]) -> tuple[int, str]:
        raise ZeroDivisionError("bug inside the check")

    output, raised, _ = _selfcheck(monkeypatch, tmp_path, rule)
    assert raised is None
    assert "自检代码自身出错" in output and "不影响主运行" in output


def test_run_folder_defaults_only_ingest_the_pdfs_of_ten_questions_whose_pdf_is_present() -> None:
    config = _code_cell("config")
    for pattern in (
        r'^QUESTION_SELECTION\s*=\s*"first"',
        r"^ONLY_QUESTION_DOCS\s*=\s*False\b",
        r"^DOC_ALIASES\s*=\s*\{\}",
        r'^ON_UNMATCHED_DOCS\s*=\s*"skip"',
        r'^MAX_LIVE_CALLS_PER_PDF\s*=\s*"auto"',
    ):
        assert re.search(pattern, config, re.MULTILINE), pattern
    run = _code_cell("run")
    for argument in (
        "max_live_calls_per_pdf=MAX_LIVE_CALLS_PER_PDF",
        "question_selection=QUESTION_SELECTION",
        "only_question_docs=ONLY_QUESTION_DOCS",
        "doc_aliases=DOC_ALIASES",
        "on_unmatched_docs=ON_UNMATCHED_DOCS",
    ):
        assert argument in run
    results = _code_cell("results")
    assert '"pages"' in results and "pages_complete" in results and "claim_blocked" in results
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    for name in ("ONLY_QUESTION_DOCS", "QUESTION_SELECTION", "DOC_ALIASES", "ON_UNMATCHED_DOCS"):
        assert name in intro


def test_status_table_shows_claim_takeover_and_explains_blocked_calls() -> None:
    results = _code_cell("results")
    assert '"claims_taken_over"' in results and "ingestion.claims_taken_over" in results
    assert "calls_claim_blocked" in results
    assert (
        "次模型调用被未完成的调用占用\uff08可能是上次被中断的运行\uff09\uff0c对应页本次未完成"
        in results
    )
    assert "约 15 分钟后重跑会自动补上\uff0c无需删除文件" in results
    assert "接管并重发了" in results and "次被中断的调用\uff08可能重复计费\uff09" in results
    assert "手工删除" not in results
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "自动接管" in intro and "无需手工删除" in intro


def test_question_docs_cell_sits_after_the_llm_selfcheck_and_before_the_run() -> None:
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("llm-selfcheck") < ids.index("question-docs") < ids.index("run")
    source = _code_cell("question-docs")
    assert "check_question_docs(" in source and "raise QuestionDocsError" in source
    assert "run_folder_pipeline" not in source


def _run_question_docs(tmp_path: Path, **config: object) -> tuple[str, BaseException | None]:
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir(parents=True)
    (pdfs / "Meridian Interim 2024.pdf").write_bytes(b"%PDF-1.7 a")
    (pdfs / "atlas.pdf").write_bytes(b"%PDF-1.7 b")
    questions = tmp_path / "questions.jsonl"
    rows = [
        {"id": "q1", "question": "Q1?", "doc": "absent report"},
        {"id": "q2", "question": "Q2?", "doc": "meridian_interim_2024"},
        {"id": "q3", "question": "Q3?", "doc": "Atlas"},
    ]
    questions.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    namespace: dict[str, Any] = {
        "PDF_DIR": pdfs,
        "QUESTIONS": questions,
        "MAX_QUESTIONS": 10,
        "QUESTION_SELECTION": "first_matched",
        "ONLY_QUESTION_DOCS": True,
        "DOC_ALIASES": {},
        "ON_UNMATCHED_DOCS": "error",
        **config,
    }
    buffer = io.StringIO()
    raised: BaseException | None = None
    with contextlib.redirect_stdout(buffer):
        try:
            exec(compile(_code_cell("question-docs"), "question-docs", "exec"), namespace)
        except ValueError as error:
            raised = error
    return buffer.getvalue(), raised


def test_question_docs_cell_lists_selected_and_skipped_questions_without_stopping(
    tmp_path: Path,
) -> None:
    printed, raised = _run_question_docs(tmp_path)
    assert raised is None
    assert "选中 q2: ['meridian_interim_2024'] → Meridian Interim 2024.pdf  [normalized]" in printed
    assert "跳过 q1: ['absent report']" in printed
    assert "不保证答得出" in printed


def test_question_docs_cell_stops_on_a_miss_when_every_question_is_asked(tmp_path: Path) -> None:
    printed, raised = _run_question_docs(tmp_path, QUESTION_SELECTION="first")
    assert raised is not None and "'absent report': 文件夹里找不到" in str(raised)
    assert "✗ 'absent report'" in printed
    _, skipped = _run_question_docs(
        tmp_path / "skip", QUESTION_SELECTION="first", ON_UNMATCHED_DOCS="skip"
    )
    assert skipped is None


def test_the_notebook_ingests_in_lite_mode_by_default_and_the_mode_chooses_the_tree() -> None:
    config = _code_cell("config")
    assert re.search(r'^INGEST_MODE\s*=\s*"lite"', config, re.MULTILINE)
    assert re.search(r"^BUILD_TREE\s*=\s*None\b", config, re.MULTILINE)
    assert '"full"' in config  # the comment says how to switch back
    run = _code_cell("run")
    assert "ingest_mode=INGEST_MODE" in run and "build_tree=BUILD_TREE" in run
    results = _code_cell("results")
    for shown in ('"mode"', "published_ingest_mode", '"skipped_calls"', "skipped_calls"):
        assert shown in results
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "INGEST_MODE" in intro and "export_document_review" in intro


def test_the_layout_policy_is_auto_by_default_and_full_keeps_the_model() -> None:
    config = _code_cell("config")
    assert re.search(r'^LAYOUT_POLICY\s*=\s*"auto"', config, re.MULTILINE)
    # The comment names every explicit value, says long reports are unverified, how to go back.
    for value in ('"model"', '"deterministic-text-pages"', '"onnx-layout"'):
        assert value in config, value
    assert "尚未在长篇财报上验证" in config
    # The choice is the library's (it calls ADR 0030's own preflight), never a local import.
    assert re.search(
        r"^EFFECTIVE_LAYOUT, LAYOUT_REASON = choose_layout_policy\(", config, re.MULTILINE
    )
    assert "ingest_mode=INGEST_MODE" in config
    assert "onnx_layout_model=settings.onnx_layout_model" in config
    assert "LAYOUT_REASON" in config.split("choose_layout_policy(", 1)[1]
    # Full ignores it (full stays byte for byte) and says so instead of ignoring it silently.
    assert "忽略 LAYOUT_POLICY" in config
    for name, source in _code_cells():
        assert "import onnxruntime" not in source, name
    run = _code_cell("run")
    assert "layout_policy=EFFECTIVE_LAYOUT" in run
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "LAYOUT_POLICY" in intro and "尚未在长篇财报上验证" in intro
    for shown in ('"auto"', '"onnx-layout"', "APP_ONNX_LAYOUT_MODEL", "pdfspine[onnx]"):
        assert shown in intro, shown


def _run_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, onnx_layout_model: str | None
) -> tuple[dict[str, Any], str]:
    monkeypatch.delenv(ONNX_MODELS_ENV, raising=False)
    stub = SimpleNamespace(
        pdf_source_dir=tmp_path / "pdfs",
        questions_path=None,
        ingestion_root=tmp_path / "ingestion",
        onnx_layout_model=onnx_layout_model,
        answer_audit_path=None,
    )
    monkeypatch.setattr(configs, "get_settings", lambda: stub)
    monkeypatch.setattr(pdfspine_tsr, "get_settings", lambda: stub)
    namespace: dict[str, Any] = {}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("config"), "config", "exec"), namespace)
    return namespace, buffer.getvalue()


def test_auto_without_onnx_weights_uses_deterministic_text_pages_and_says_how_to_enable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace, output = _run_config(monkeypatch, tmp_path, None)
    assert namespace["LAYOUT_POLICY"] == "auto"
    assert namespace["EFFECTIVE_LAYOUT"] == "deterministic-text-pages"
    assert "APP_ONNX_LAYOUT_MODEL" in output and "pdfspine[onnx]" in output
    # The table structure follows the same weights: none configured keeps the verbatim rows.
    assert namespace["UNVERIFIED_TABLE_STRUCTURE"] == "auto"
    assert namespace["EFFECTIVE_TABLE_STRUCTURE"] == "rows"
    assert "slanet-plus.onnx" in output


@pytest.mark.usefixtures("onnx_runtime_present")
def test_auto_with_onnx_weights_uses_the_onnx_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    weights = tmp_path / ONNX_LAYOUT_MODEL_FILE
    weights.write_bytes(b"fake-weights")
    namespace, output = _run_config(monkeypatch, tmp_path, str(weights))
    assert namespace["EFFECTIVE_LAYOUT"] == "onnx-layout"
    assert "onnx-layout" in output and "auto" in output
    # Layout weights alone: the structure model is missing from that directory, rows stay.
    assert namespace["EFFECTIVE_TABLE_STRUCTURE"] == "rows"
    (tmp_path / pdfspine_tsr.MODEL_FILE).write_bytes(b"fake-structure-weights")
    namespace, output = _run_config(monkeypatch, tmp_path, str(weights))
    assert namespace["EFFECTIVE_TABLE_STRUCTURE"] == "tsr" and "tsr" in output


def test_the_table_structure_is_auto_by_default_and_full_keeps_the_rows() -> None:
    config = _code_cell("config")
    assert re.search(r'^UNVERIFIED_TABLE_STRUCTURE\s*=\s*"auto"', config, re.MULTILINE)
    assert '"rows"' in config and '"tsr"' in config
    assert re.search(
        r"^EFFECTIVE_TABLE_STRUCTURE, TABLE_STRUCTURE_REASON = choose_unverified_table_structure\(",
        config,
        re.MULTILINE,
    )
    assert "ingest_mode=INGEST_MODE" in config.split("choose_unverified_table_structure(", 1)[1]
    assert "TABLE_STRUCTURE_REASON" in config.split("choose_unverified_table_structure(", 1)[1]
    assert "忽略 UNVERIFIED_TABLE_STRUCTURE" in config
    assert "unverified_table_structure=EFFECTIVE_TABLE_STRUCTURE" in _code_cell("run")
    results = _code_cell("results")
    for shown in ("table_tsr_grids", "table_tsr_fallbacks", "table_tsr_fallback_reasons"):
        assert shown in results, shown
    assert "推断网格" in results and "回落" in results
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "UNVERIFIED_TABLE_STRUCTURE" in intro and "待核验" in intro and "精确匹配" in intro


def test_the_status_shows_partition_row_table_and_embedding_counts_per_pdf() -> None:
    results = _code_cell("results")
    for shown in (
        "pages_partitioned_deterministically",
        "pages_partition_model_fallback",
        "partition_fallback_reasons",
        "table_row_transcriptions",
        "table_row_lines",
        "embedding_requests",
        "embedded_objects",
    ):
        assert shown in results, shown
    assert "确定性处理" in results and "回退模型" in results
    assert "按行收录" in results and "embedding 请求" in results


def test_the_status_shows_onnx_pages_and_the_index_text_layout_per_pdf() -> None:
    results = _code_cell("results")
    for shown in (
        "pages_partitioned_onnx",
        "row_unit_tables",
        "row_units",
        "unscored_running_members",
    ):
        assert shown in results, shown
    assert "ONNX" in results and "行单元" in results and "页眉页脚" in results


# ───────────── testbench: 答题之后的检索测试台(零模型调用, 只读审计库) ─────────────


def test_the_testbench_cell_follows_the_answers_and_writes_beside_them() -> None:
    ids = [cell_id for cell_id, _ in _code_cells()]
    assert ids.index("answers") + 1 == ids.index("testbench")
    source = _code_cell("testbench")
    assert 'settings.answer_audit_path or (INGESTION_ROOT / "answers-audit.sqlite")' in source
    assert "run_retrieval_testbench(" in source
    for argument in (
        "report=result",
        "max_questions=MAX_QUESTIONS",
        "question_selection=QUESTION_SELECTION",
        "folder=PDF_DIR",
        "doc_aliases=DOC_ALIASES",
        "ingestion_root=INGESTION_ROOT",
    ):
        assert argument in source, argument
    assert "print(format_table(bench))" in source
    assert "write_testbench(bench, REPORT_DIR)" in source
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "testbench.csv" in intro and "重跑一次答题即可补齐" in intro


def _pipeline_result(tmp_path: Path, *, answered: bool) -> FolderPipelineResult:
    """A real (beartype-checked) run result: one unrouted case, or no question set."""
    case = EvalCase(
        case_id="q1",
        question="Revenue?",
        document_id=None,
        verdict="FAIL",
        failures=("routing_failed",),
    )
    summary = EvalSummary(format="questions", totals={}, metrics={}, cases=(case,))
    return FolderPipelineResult(
        folder=str(tmp_path / "pdfs"),
        ingestion_root=str(tmp_path / "ingestion"),
        documents=(),
        eval=summary if answered else None,
        live_calls=LiveCalls(),
        budget_exhausted=False,
    )


def _run_testbench(
    tmp_path: Path, result: FolderPipelineResult, audit: Path, questions: Path | None
) -> str:
    namespace: dict[str, Any] = {
        "result": result,
        "settings": SimpleNamespace(answer_audit_path=audit),
        "INGESTION_ROOT": tmp_path / "ingestion",
        "QUESTIONS": questions,
        "MAX_QUESTIONS": None,
        "QUESTION_SELECTION": "first",
        "PDF_DIR": tmp_path / "pdfs",
        "DOC_ALIASES": {},
        "REPORT_DIR": tmp_path / "reports",
    }
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("testbench"), "testbench", "exec"), namespace)
    return buffer.getvalue()


def _one_question(tmp_path: Path) -> Path:
    questions = tmp_path / "set.jsonl"
    questions.write_text('{"id": "q1", "question": "Revenue?", "doc": "a.pdf"}\n', encoding="utf-8")
    return questions


def test_the_testbench_skips_without_questions_or_a_readable_journal(tmp_path: Path) -> None:
    questions = _one_question(tmp_path)
    answered = _pipeline_result(tmp_path, answered=True)
    unanswered = _pipeline_result(tmp_path, answered=False)
    assert "跳过检索测试台" in _run_testbench(tmp_path, unanswered, tmp_path / "a.db", None)
    missing = _run_testbench(tmp_path, answered, tmp_path / "missing.sqlite", questions)
    assert "跳过检索测试台" in missing
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database" * 100)
    assert "跳过检索测试台" in _run_testbench(tmp_path, answered, corrupt, questions)
    assert not (tmp_path / "reports").exists()


def test_the_testbench_prints_and_writes_its_table_from_an_empty_journal(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "answers-audit.sqlite"
    AnswerAuditStore(audit)
    answered = _pipeline_result(tmp_path, answered=True)
    output = _run_testbench(tmp_path, answered, audit, _one_question(tmp_path))
    assert "routing_failed" in output
    assert sorted(path.name for path in (tmp_path / "reports").iterdir()) == [
        "testbench.csv",
        "testbench.json",
    ]


def test_the_cases_cell_shows_how_many_questions_crossed_and_where_they_cited(
    tmp_path: Path,
) -> None:
    """ADR 0032: every question searched every PDF; the cell says so and names the cited PDFs."""
    meridian, orion = "a" * 64, "b" * 64
    cases = (
        EvalCase(
            case_id="q1",
            question="Revenue?",
            document_id=orion,
            verdict="answered",
            failures=(),
            routing="cross_document",
            searched_documents=2,
            cited_documents=(orion,),
            expected_doc=None,
        ),
        EvalCase(
            case_id="q2",
            question="Margin?",
            document_id=meridian,
            verdict="answered",
            failures=(),
            routing="cross_document",
            searched_documents=2,
            cited_documents=(meridian,),
            expected_doc=meridian,
            cited_doc_hit=True,
        ),
    )
    result = FolderPipelineResult(
        folder=str(tmp_path / "pdfs"),
        ingestion_root=str(tmp_path / "ingestion"),
        documents=tuple(
            DocumentRun(pdf_path=str(tmp_path / "pdfs" / name), sha256=sha, status="published")
            for name, sha in (("meridian.pdf", meridian), ("orion.pdf", orion))
        ),
        eval=EvalSummary(format="questions", totals={}, metrics={}, cases=cases),
        live_calls=LiveCalls(),
        budget_exhausted=False,
    )

    def show(rows: list[dict[str, object]], columns: list[str]) -> None:
        print(" ".join(columns))  # noqa: T201 — stands in for the notebook's own table printer
        for row in rows:
            print(" | ".join(str(row.get(column, "")) for column in columns))  # noqa: T201

    namespace: dict[str, Any] = {
        "result": result,
        "MAX_QUESTIONS": None,
        "Path": Path,
        "show": show,
    }
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("cases"), "cases", "exec"), namespace)
    printed = buffer.getvalue()
    assert "2 道题在 2 份已发布 PDF 里跨文档检索作答" in printed
    assert "docs cited_docs cited_doc_hit" in printed
    assert "q1 | answered |  | 0 | 2 | orion.pdf |  |" in printed
    assert "q2 | answered |  | 0 | 2 | meridian.pdf | True |" in printed


def test_the_question_docs_cell_says_a_miss_is_answered_across_every_pdf(tmp_path: Path) -> None:
    printed, raised = _run_question_docs(
        tmp_path, QUESTION_SELECTION="first", ONLY_QUESTION_DOCS=False, ON_UNMATCHED_DOCS="skip"
    )
    assert raised is None
    assert "检索本身在所有已入库 PDF 中进行" in printed
    assert "'absent report': 文件夹里找不到" in printed
    assert "将改为在所有已入库 PDF 中跨文档检索作答" in printed
    source = _code_cell("question-docs")
    assert "对不上的题将改为跨文档检索作答" in source


# ───────────── 并发入库 (ADR 0033) 与临时错误 (ADR 0035) ─────────────


def test_parallel_documents_default_to_four_with_the_reasons_and_reach_the_run() -> None:
    config = _code_cell("config")
    assert re.search(r"^MAX_PARALLEL_DOCUMENTS\s*=\s*4\b", config, re.MULTILINE)
    # The comment gives ADR 0033's grounds and how to go back to one at a time.
    for reason in ("同时只有一个请求", "Azure", "Workspace", "内存", "= 1", "串行"):
        assert reason in config, reason
    assert "max_parallel_documents=MAX_PARALLEL_DOCUMENTS" in _code_cell("run")
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "MAX_PARALLEL_DOCUMENTS" in intro and "slot" in intro


def test_document_filter_defaults_to_none_and_reaches_the_run() -> None:
    config = _code_cell("config")
    assert re.search(r'^NB_DOCUMENT_FILTER\s*=\s*""', config, re.MULTILINE)
    # The comment says the form (JSON), the semantics and that nothing is inferred from questions.
    for words in ("JSON", "documents.csv", "APP_DOCUMENT_TAG_PATH_TEMPLATE", "不会从题集"):
        assert words in config, words
    assert "document_filter=NB_DOCUMENT_FILTER" in _code_cell("run")


def _run_progress(
    monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, dict[str, object]]]
) -> str:
    """Execute the run cell against a stand-in pipeline that only emits ``events``."""
    from enterprise_pdf_rag.adapters import folder_pipeline

    seen: dict[str, object] = {}

    def fake(folder: object, **kwargs: object) -> str:
        seen.update(kwargs)
        progress = kwargs["progress"]
        assert callable(progress)
        for event, payload in events:
            progress(event, payload)
        return "result"

    monkeypatch.setattr(folder_pipeline, "run_folder_pipeline", fake)
    names = [
        "PDF_DIR",
        "QUESTIONS",
        "INGESTION_ROOT",
        "MAX_LIVE_CALLS_PER_PDF",
        "MAX_LIVE_CALLS_TOTAL",
        "MAX_QUESTIONS",
        "QUESTION_SELECTION",
        "ONLY_QUESTION_DOCS",
        "DOC_ALIASES",
        "ON_UNMATCHED_DOCS",
        "INGEST_MODE",
        "BUILD_TREE",
        "EFFECTIVE_TABLE_STRUCTURE",
        "EFFECTIVE_LAYOUT",
        "REQUALIFY",
        "REPORT_DIR",
        "NB_DOCUMENT_FILTER",
    ]
    namespace: dict[str, Any] = dict.fromkeys(names)
    namespace["MAX_PARALLEL_DOCUMENTS"] = 4
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("run"), "run", "exec"), namespace)
    assert seen["max_parallel_documents"] == 4
    return buffer.getvalue()


def test_progress_lines_name_their_slot_and_say_what_stopping_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _run_progress(
        monkeypatch,
        [
            ("discovered", {"pdfs": 2}),
            ("document_start", {"pdf": "a.pdf", "budget": 10, "slot": 2}),
            ("document_done", {"pdf": "a.pdf", "status": "published", "slot": 2}),
            ("stopping", {"reason": "interrupted", "running": 1}),
        ],
    ).splitlines()
    assert output[0] == "discovered {'pdfs': 2}"  # no slot: printed exactly as before
    assert output[1].startswith("[slot 2] document_start ")
    assert "'slot'" not in output[1] and "'a.pdf'" in output[1]
    assert output[2].startswith("[slot 2] document_done ")
    assert output[3].startswith("stopping ")
    assert "正在停止\uff1a排队的文档不再开始\uff0c进行中的文档在当前页结束后停止" in output[4]


def _run_results(result: FolderPipelineResult) -> str:
    namespace: dict[str, Any] = {"result": result}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_code_cell("results"), "results", "exec"), namespace)
    return buffer.getvalue()


def test_the_status_shows_transient_retries_and_explains_a_rerun_heals_them(
    tmp_path: Path,
) -> None:
    results = _code_cell("results")
    assert '"retries"' in results and '"transient_failures"' in results

    def run_with(retries: int, failures: int) -> str:
        document = DocumentRun(
            pdf_path=str(tmp_path / "a.pdf"),
            sha256="0" * 64,
            status="published",
            retries=retries,
            transient_failures=failures,
        )
        result = FolderPipelineResult(
            folder=str(tmp_path),
            ingestion_root=str(tmp_path / "ingestion"),
            documents=(document,),
            eval=None,
            live_calls=LiveCalls(),
            budget_exhausted=False,
        )
        return _run_results(result)

    hint = "2 次调用因限流 / 服务端错误在本轮重试耗尽\uff0c对应页本轮未完成\uff1b重跑会自动重试\uff0c无需删除文件"
    assert hint in run_with(5, 2)
    assert "重试耗尽" not in run_with(3, 0)
    intro = _source(next(cell for cell in _notebook()["cells"] if cell["id"] == "intro"))
    assert "transient_failures" in intro and "429" in intro
