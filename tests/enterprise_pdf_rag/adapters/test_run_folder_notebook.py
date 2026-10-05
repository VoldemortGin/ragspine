"""The example notebook is valid nbformat 4, ships without outputs and carries no secrets."""

import contextlib
import csv
import errno
import io
import json
import os
import re
from collections.abc import Callable
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


_Rule = Callable[[dict[str, Any]], tuple[int, str]]


class _Net:
    """http.client.HTTPSConnection 的替身: 不开 socket, 记录请求, 按 rule(body) 脚本化回复。"""

    def __init__(self, rule: _Rule, error: BaseException | None = None) -> None:
        self.rule = rule
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.targets: list[tuple[str, str]] = []
        self.authorizations: list[str] = []

    def connection(self) -> type:
        net = self

        class _Response:
            def __init__(self, status: int, text: str) -> None:
                self.status = status
                self._text = text.encode()

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
                status, text = net.rule(self._body)
                return _Response(status, text)

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
    assert "[连接失败]" in output and output.count("[连接失败]") == 1
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
        r'^QUESTION_SELECTION\s*=\s*"first_matched"',
        r"^ONLY_QUESTION_DOCS\s*=\s*True\b",
        r"^DOC_ALIASES\s*=\s*\{\}",
        r'^ON_UNMATCHED_DOCS\s*=\s*"error"',
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
    assert "次模型调用被未完成的调用占用（可能是上次被中断的运行），对应页本次未完成" in results
    assert "约 15 分钟后重跑会自动补上，无需删除文件" in results
    assert "接管并重发了" in results and "次被中断的调用（可能重复计费）" in results
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
