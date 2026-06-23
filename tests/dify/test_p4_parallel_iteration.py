"""P4 codegen（并行 + iteration）验收：golden 快照 + exec 跑通。

parallel_layer 内≥2 独立重节点 → ThreadPoolExecutor；iteration 串行 for / 并行
ThreadPoolExecutor(max_workers=parallel_nums)。LLM 走 MockProvider（离线确定性）。
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ragspine import MockProvider
from ragspine.dify.codegen.emitter import GeneratedCode, generate_code
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.parse.loader import parse_dify_yaml

GOLDEN_DIR = Path(__file__).parent / "golden"


def _gen(fixture_text: Callable[[str], str], name: str) -> GeneratedCode:
    return generate_code(lower_to_ir(parse_dify_yaml(fixture_text(name))))


def _exec(code: GeneratedCode) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(code.source, "<dify>", "exec"), ns)  # noqa: S102
    return ns


def _check_golden(name: str, source: str) -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / f"{name}.py.txt"
    if not path.exists():
        path.write_text(source, encoding="utf-8")
        return
    assert source == path.read_text(encoding="utf-8"), (
        f"{name} 生成代码与 golden 不一致；预期变更则删除 {path} 重跑刷新。"
    )


# ---- golden + 合法性 ------------------------------------------------------


@pytest.mark.parametrize("name", ["parallel", "iteration"])
def test_golden_snapshot(fixture_text: Callable[[str], str], name: str) -> None:
    code = _gen(fixture_text, name)
    ast.parse(code.source)  # 合法 Python
    _check_golden(name, code.source)


# ---- 并行层 → ThreadPoolExecutor ------------------------------------------


def test_parallel_layer_uses_threadpool(fixture_text: Callable[[str], str]) -> None:
    code = _gen(fixture_text, "parallel")
    assert "from concurrent.futures import ThreadPoolExecutor" in code.imports
    assert "ThreadPoolExecutor(max_workers=2)" in code.source
    assert "def _task_llm_a()" in code.source
    assert "def _task_llm_b()" in code.source
    # 家族全同步：绝不生成 async。
    assert "async def" not in code.source
    assert "await " not in code.source


def test_parallel_runs_and_joins(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text, "parallel"))
    out = ns["run_workflow"](ns["Inputs"](topic="AI"), provider=MockProvider())
    assert "result" in out
    # tt_join 合并了两路并发 LLM 的输出。
    assert "正面：" in out["result"] and "反面：" in out["result"]


# ---- iteration → 并行/串行 -----------------------------------------------


def test_iteration_parallel_uses_threadpool(fixture_text: Callable[[str], str]) -> None:
    code = _gen(fixture_text, "iteration")
    assert "from concurrent.futures import ThreadPoolExecutor" in code.imports
    assert "ThreadPoolExecutor(max_workers=5)" in code.source  # parallel_nums=5
    assert "def _iter_body_iter_1(" in code.source
    assert "async def" not in code.source


def test_iteration_runs_over_items(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text, "iteration"))
    out = ns["run_workflow"](ns["Inputs"](items=["x", "y", "z"]), provider=MockProvider())
    assert "results" in out
    assert isinstance(out["results"], list)
    assert len(out["results"]) == 3  # 每项一条


def test_iteration_empty_items(fixture_text: Callable[[str], str]) -> None:
    """空数组 / None → 空结果，不崩。"""
    ns = _exec(_gen(fixture_text, "iteration"))
    out = ns["run_workflow"](ns["Inputs"](items=[]), provider=MockProvider())
    assert out["results"] == []
    out_none = ns["run_workflow"](ns["Inputs"](items=None), provider=MockProvider())
    assert out_none["results"] == []


def test_iteration_serial_when_not_parallel() -> None:
    """is_parallel=false → 生成串行列表推导（不引入 ThreadPoolExecutor）。"""
    dsl = """
app:
  mode: workflow
  name: iter-serial
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: items, type: array}]}
      - id: iter_1
        data:
          type: iteration
          title: 串行迭代
          iterator_selector: [start_1, items]
          output_selector: [iter_inner, text]
          is_parallel: false
          start_node_id: iter_inner
      - id: iter_inner
        data:
          type: llm
          title: 内层
          iteration_id: iter_1
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "处理 {{#iter_1.item#}}"}]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [iter_1, output]}]}
    edges:
      - {source: start_1, target: iter_1, sourceHandle: source}
      - {source: iter_1, target: end_1, sourceHandle: source}
"""
    code = generate_code(lower_to_ir(parse_dify_yaml(dsl)))
    ast.parse(code.source)
    # 串行：不【导入】也不【使用】线程池（docstring 里出现该词不算）。
    assert "from concurrent.futures import ThreadPoolExecutor" not in code.imports
    assert "with ThreadPoolExecutor" not in code.source
    assert "for _it in _iter_items_iter_1" in code.source
    ns = _exec(code)
    out = ns["run_workflow"](ns["Inputs"](items=["a", "b"]), provider=MockProvider())
    assert len(out["out"]) == 2
