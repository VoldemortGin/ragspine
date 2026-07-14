"""P6 门面 + CLI 验收 + 编译器不变量 conformance（corespine 基座）。

- compile_dify_yaml 返回 CompileResult(code, suggestions, ir)；analyze 返回 Suggestion 列表。
- CLI `ragspine dify compile/analyze <path>` 端到端：吃 fixture → 出代码 + 建议。
- conformance：用 corespine ConformanceSuite × InvariantPack 给「编译器不变量」（生成代码
  AST 合法 / 只用家族原语 import 白名单 / 有 run_workflow 入口 / 不生成 async）写参数化测试，
  四 fixture × 四不变量笛卡尔积逐格校验。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from corespine import ConformanceSuite, InvariantPack

from ragspine import MockProvider
from ragspine.cli import main
from ragspine.dify import (
    CompileResult,
    GeneratedCode,
    analyze,
    compile_dify_yaml,
)
from ragspine.dify.errors import DifyCompileError
from ragspine.workflows import dump_dify_yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FIXTURES = ("seq", "branch", "parallel", "iteration")

_IMPORT_WHITELIST = frozenset(
    {
        "__future__",
        "dataclasses",
        "typing",
        "string",
        "concurrent",
        "corespine",
        "ragspine",
    }
)


# ---------------------------------------------------------------------------
# 门面 compile_dify_yaml / analyze
# ---------------------------------------------------------------------------


def test_compile_returns_full_result(fixtures_dir: Path) -> None:
    result = compile_dify_yaml(fixtures_dir / "parallel.yml")
    assert isinstance(result, CompileResult)
    assert isinstance(result.code, GeneratedCode)
    assert result.code.entrypoint == "run_workflow"
    assert result.ir.mode == "workflow"
    # parallel fixture → 至少一条 PARALLEL_001 建议。
    assert any(s.rule_id == "PARALLEL_001" for s in result.suggestions)


def test_compile_no_analyze(fixtures_dir: Path) -> None:
    result = compile_dify_yaml(fixtures_dir / "seq.yml", analyze=False)
    assert result.suggestions == ()


def test_compile_provider_expr_propagates(fixtures_dir: Path) -> None:
    """provider_expr 进入生成代码的 provider 默认值。"""
    result = compile_dify_yaml(fixtures_dir / "seq.yml", provider_expr="MockProvider(prefix='x')")
    assert "MockProvider(prefix='x')" in result.code.source


def test_compile_rejects_unknown_target(fixtures_dir: Path) -> None:
    with pytest.raises(DifyCompileError) as ei:
        compile_dify_yaml(fixtures_dir / "seq.yml", target="nonexistent")
    assert ei.value.code == "dify.unsupported_target"


def test_analyze_facade(fixtures_dir: Path) -> None:
    suggestions = analyze(fixtures_dir / "parallel.yml")
    assert any(s.rule_id == "PARALLEL_001" for s in suggestions)


def test_compiled_code_runs_end_to_end(fixtures_dir: Path) -> None:
    """端到端铁律：fixture → compile → exec → run_workflow(Inputs, MockProvider) 离线跑通。"""
    result = compile_dify_yaml(fixtures_dir / "seq.yml")
    ns: dict[str, object] = {}
    exec(compile(result.code.source, "<e2e>", "exec"), ns)  # noqa: S102
    run = ns["run_workflow"]
    inputs_cls = ns["Inputs"]
    out = run(inputs_cls(question="香港REVENUE多少"), provider=MockProvider())  # type: ignore[operator]
    assert "result" in out


# ---------------------------------------------------------------------------
# CLI：ragspine dify compile / analyze
# ---------------------------------------------------------------------------


def test_cli_compile_prints_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["dify", "compile", str(FIXTURES_DIR / "seq.yml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "def run_workflow(" in out
    assert "provider.chat(" in out


def test_cli_compile_no_analyze(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["dify", "compile", str(FIXTURES_DIR / "parallel.yml"), "--no-analyze"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "静态优化建议" not in err


def test_cli_analyze_prints_suggestions(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["dify", "analyze", str(FIXTURES_DIR / "parallel.yml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PARALLEL_001" in out


def test_cli_compile_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["dify", "compile", "/definitely/not/here.yml"])
    assert rc == 2
    assert "不存在" in capsys.readouterr().err


def test_cli_compile_invalid_dsl(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("app:\n  mode: chat\n", encoding="utf-8")  # 不支持的 mode
    rc = main(["dify", "compile", str(bad)])
    assert rc == 1
    assert "编译失败" in capsys.readouterr().err


def test_cli_compile_accepts_equivalent_json_yaml_and_toml(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    workflow: dict[str, object] = {
        "app": {"mode": "workflow", "name": "format-demo"},
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "graph": {
                "nodes": [
                    {
                        "id": "start_1",
                        "data": {
                            "type": "start",
                            "title": "Start",
                            "variables": [],
                        },
                    },
                    {
                        "id": "end_1",
                        "data": {"type": "end", "title": "End", "outputs": []},
                    },
                ],
                "edges": [
                    {
                        "source": "start_1",
                        "target": "end_1",
                        "sourceHandle": "source",
                    }
                ],
            }
        },
    }
    documents = {
        "workflow.json": json.dumps(workflow),
        "workflow.yml": dump_dify_yaml(workflow),
        "workflow.toml": """
kind = "app"
version = "0.6.0"

[app]
mode = "workflow"
name = "format-demo"

[workflow.graph]

[[workflow.graph.nodes]]
id = "start_1"
[workflow.graph.nodes.data]
type = "start"
title = "Start"
variables = []

[[workflow.graph.nodes]]
id = "end_1"
[workflow.graph.nodes.data]
type = "end"
title = "End"
outputs = []

[[workflow.graph.edges]]
source = "start_1"
target = "end_1"
sourceHandle = "source"
""",
    }

    generated: list[str] = []
    for filename, document in documents.items():
        path = tmp_path / filename
        path.write_text(document, encoding="utf-8")
        assert main(["dify", "compile", str(path), "--no-analyze"]) == 0
        generated.append(capsys.readouterr().out)

    assert generated[0] == generated[1] == generated[2]
    assert "def run_workflow(" in generated[0]


# ---------------------------------------------------------------------------
# 编译器不变量 conformance（corespine ConformanceSuite × InvariantPack）
# ---------------------------------------------------------------------------


def _make_code_factory(name: str) -> Callable[[], GeneratedCode]:
    def _factory() -> GeneratedCode:
        return compile_dify_yaml(FIXTURES_DIR / f"{name}.yml").code

    return _factory


def _inv_valid_ast(code: GeneratedCode) -> None:
    ast.parse(code.source)  # 非法语法在此抛 SyntaxError


def _inv_import_whitelist(code: GeneratedCode) -> None:
    tree = ast.parse(code.source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in _IMPORT_WHITELIST, f"非白名单 import：{node.module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in _IMPORT_WHITELIST, f"非白名单 import：{alias.name}"


def _inv_has_entrypoint(code: GeneratedCode) -> None:
    tree = ast.parse(code.source)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert code.entrypoint in funcs, f"缺入口函数 {code.entrypoint}"


def _inv_no_async(code: GeneratedCode) -> None:
    tree = ast.parse(code.source)
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.AsyncFunctionDef, ast.Await)), "家族全同步，不得生成 async"


_SUITE: ConformanceSuite[GeneratedCode] = ConformanceSuite(
    implementations={name: _make_code_factory(name) for name in _FIXTURES},
    pack=(
        InvariantPack[GeneratedCode]("dify-codegen")
        .add("valid_ast", _inv_valid_ast)
        .add("import_whitelist", _inv_import_whitelist)
        .add("has_entrypoint", _inv_has_entrypoint)
        .add("no_async", _inv_no_async)
    ),
)


@pytest.mark.parametrize(**_SUITE.parametrize_kwargs())
def test_codegen_conformance(case: Callable[[], None]) -> None:
    """四 fixture × 四编译器不变量笛卡尔积逐格校验（corespine conformance 基座）。"""
    case()
