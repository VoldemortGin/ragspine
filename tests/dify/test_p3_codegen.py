"""P3 codegen（顺序 + 条件）验收：golden 快照 + exec 后 run_workflow 跑通。

支持 start/end/answer/llm/code/if-else/template-transform。LLM 走 MockProvider（离线确定性）。
golden 快照存 tests/dify/golden/<name>.py.txt；缺失则首跑生成（同时断言可 exec），后续对比。
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ragspine import MockProvider
from ragspine.dify.codegen.emitter import GeneratedCode, generate_code
from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.parse.loader import parse_dify_yaml

GOLDEN_DIR = Path(__file__).parent / "golden"


def _gen(fixture_text: Callable[[str], str], name: str) -> GeneratedCode:
    ir = lower_to_ir(parse_dify_yaml(fixture_text(name)))
    return generate_code(ir)


def _exec(code: GeneratedCode) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(code.source, f"<dify:{code.entrypoint}>", "exec"), ns)  # noqa: S102
    return ns


def _check_golden(name: str, source: str) -> None:
    """对比 golden 快照；缺失则写入（首跑确立基线）。"""
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / f"{name}.py.txt"
    if not path.exists():
        path.write_text(source, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert source == expected, (
        f"{name} 生成代码与 golden 快照不一致；若属预期变更，删除 "
        f"{path} 后重跑以刷新基线。"
    )


# ---- 生成代码合法性 + golden 快照 ----------------------------------------


@pytest.mark.parametrize("name", ["seq", "branch"])
def test_generated_code_is_valid_python(
    fixture_text: Callable[[str], str], name: str
) -> None:
    """生成代码是合法 Python（AST 可解析）且 import 面只用家族原语。"""
    code = _gen(fixture_text, name)
    tree = ast.parse(code.source)  # 非法语法会在此抛
    assert isinstance(tree, ast.Module)
    # import 白名单：只允许 stdlib + corespine + ragspine。
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in {
                "__future__", "dataclasses", "typing", "string",
                "concurrent", "corespine", "ragspine",
            }, f"非白名单 import：{node.module}"


@pytest.mark.parametrize("name", ["seq", "branch"])
def test_golden_snapshot(fixture_text: Callable[[str], str], name: str) -> None:
    _check_golden(name, _gen(fixture_text, name).source)


def test_codegen_is_deterministic(fixture_text: Callable[[str], str]) -> None:
    """同一 fixture 两次生成字节级一致（离线可复现）。"""
    a = _gen(fixture_text, "seq").source
    b = _gen(fixture_text, "seq").source
    assert a == b


# ---- exec 后跑通 ----------------------------------------------------------


def test_seq_runs(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text, "seq"))
    out = ns["run_workflow"](ns["Inputs"](question="香港REVENUE多少"), provider=MockProvider())
    assert "result" in out
    assert out["result"].startswith("回答：")


def test_branch_takes_true_path(fixture_text: Callable[[str], str]) -> None:
    """score>60 → 走 if 分支（llm_yes 执行，llm_no 不执行）。"""
    ns = _exec(_gen(fixture_text, "branch"))
    out = ns["run_workflow"](ns["Inputs"](score=90), provider=MockProvider())
    assert "answer" in out
    assert out["answer"]  # 非空（llm_yes 产出）


def test_branch_takes_false_path(fixture_text: Callable[[str], str]) -> None:
    """score<=60 → 走 else 分支（llm_no 执行）。"""
    ns = _exec(_gen(fixture_text, "branch"))
    out = ns["run_workflow"](ns["Inputs"](score=30), provider=MockProvider())
    assert "answer" in out
    assert out["answer"]


def test_default_provider_is_mock(fixture_text: Callable[[str], str]) -> None:
    """不传 provider 时默认用 MockProvider（离线、零 key 可跑）。"""
    ns = _exec(_gen(fixture_text, "seq"))
    out = ns["run_workflow"](ns["Inputs"](question="x"))
    assert "result" in out


# ---- naming 确定性 --------------------------------------------------------


def test_naming_sanitizes_and_dedups() -> None:
    names = NameTable(["1710000000000", "llm_1", "if-else", "class"])
    assert names.var("llm_1") == "llm_1"
    # 数字开头 → 前缀；非法字符 → _；关键字 → 加后缀。
    assert names.var("1710000000000").isidentifier()
    assert names.var("if-else").isidentifier()
    assert names.var("class").isidentifier()


def test_naming_collision_dedup() -> None:
    names = NameTable(["a-b", "a_b"])
    v1, v2 = names.var("a-b"), names.var("a_b")
    assert v1 != v2  # 归一后会撞 → 加后缀去重
