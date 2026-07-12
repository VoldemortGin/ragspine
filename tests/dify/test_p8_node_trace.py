"""P8 节点级 execution trace（emit_node_traces 开关）验收。

契约：generate_code(..., emit_node_traces=True) 时，生成模块带 _NODE_TRACES /
_TRACE_CLOCK / _TRACE_NODES；run_workflow 逐节点记 trace（succeeded/failed），
返回前 sweep 未执行节点为 skipped。默认 False 时生成源码与现状字节相同
（golden 回归由 test_p3_codegen 兜底，这里只断言无 trace 符号）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from ragspine import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.codegen.emitter import GeneratedCode, generate_code
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.parse.loader import parse_dify_yaml

# code 节点 main 里 raise：用于失败路径（失败记录 + 异常照抛 + 无 sweep）。
FAIL_YAML = """
app:
  mode: workflow
  name: fail-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - variable: question
              label: 问题
              type: text-input
              required: true
      - id: code_1
        data:
          type: code
          title: 会炸的代码
          code: "def main(x):\\n    raise ValueError('boom')\\n"
          code_language: python3
          variables:
            - variable: x
              value_selector: [start_1, question]
          outputs:
            out: {type: string}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - variable: out
              value_selector: [code_1, out]
    edges:
      - {source: start_1, target: code_1, sourceHandle: source}
      - {source: code_1, target: end_1, sourceHandle: source}
"""


def _gen(yaml_text: str, **kw: Any) -> GeneratedCode:
    ir = lower_to_ir(parse_dify_yaml(yaml_text))
    return generate_code(ir, **kw)


def _exec(code: GeneratedCode) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(code.source, f"<dify:{code.entrypoint}>", "exec"), ns)  # noqa: S102
    return ns


def _traces(ns: dict[str, Any]) -> list[dict[str, Any]]:
    return list(ns["_NODE_TRACES"])


# ---- 默认关：源码无 trace 符号（字节级不变由 golden 测试兜底） ----------------


def test_default_flag_off_source_has_no_trace_symbols(
    fixture_text: Callable[[str], str],
) -> None:
    code = _gen(fixture_text("seq"))
    assert "_NODE_TRACES" not in code.source
    assert "_trace_done" not in code.source
    # compile_dify_yaml 默认同样关。
    compiled = compile_dify_yaml(fixture_text("seq"))
    assert "_NODE_TRACES" not in compiled.code.source


def test_compile_dify_yaml_passthrough(fixture_text: Callable[[str], str]) -> None:
    compiled = compile_dify_yaml(fixture_text("seq"), emit_node_traces=True)
    assert "_NODE_TRACES" in compiled.code.source


# ---- seq：拓扑序、全 succeeded、inputs/outputs 快照 --------------------------


def test_seq_traces_in_topo_order(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text("seq"), emit_node_traces=True))
    ns["_TRACE_CLOCK"] = time.perf_counter
    out = ns["run_workflow"](ns["Inputs"](question="你好"), provider=MockProvider())
    assert "result" in out
    traces = _traces(ns)
    assert [t["node_id"] for t in traces] == ["start_1", "llm_1", "tt_1", "end_1"]
    assert all(t["status"] == "succeeded" for t in traces)
    assert all(t["error"] is None for t in traces)
    assert all(isinstance(t["elapsed_ms"], float) and t["elapsed_ms"] >= 0.0 for t in traces)

    by_id = {t["node_id"]: t for t in traces}
    llm = by_id["llm_1"]
    assert llm["title"] == "应答模型"
    assert llm["node_type"] == "llm"
    assert llm["inputs"] == {"start_1.question": "你好"}
    assert "text" in llm["outputs"]
    # start 无依赖 → inputs None。
    assert by_id["start_1"]["inputs"] is None
    assert by_id["start_1"]["node_type"] == "start"


def test_trace_clock_none_means_zero_elapsed(
    fixture_text: Callable[[str], str],
) -> None:
    ns = _exec(_gen(fixture_text("seq"), emit_node_traces=True))
    assert ns["_TRACE_CLOCK"] is None
    ns["run_workflow"](ns["Inputs"](question="x"), provider=MockProvider())
    assert all(t["elapsed_ms"] == 0.0 for t in _traces(ns))


def test_rerun_does_not_accumulate(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text("seq"), emit_node_traces=True))
    ns["run_workflow"](ns["Inputs"](question="a"), provider=MockProvider())
    first = len(_traces(ns))
    ns["run_workflow"](ns["Inputs"](question="b"), provider=MockProvider())
    assert len(_traces(ns)) == first


# ---- branch：走到的分支 succeeded、未走到的 skipped、if-else 自身有记录 --------


def test_branch_skipped_and_ifelse_record(
    fixture_text: Callable[[str], str],
) -> None:
    ns = _exec(_gen(fixture_text("branch"), emit_node_traces=True))
    ns["run_workflow"](ns["Inputs"](score=90), provider=MockProvider())
    traces = _traces(ns)
    by_id = {t["node_id"]: t for t in traces}

    assert by_id["ifelse_1"]["status"] == "succeeded"
    assert by_id["ifelse_1"]["node_type"] == "if-else"
    assert by_id["llm_yes"]["status"] == "succeeded"

    skipped = by_id["llm_no"]
    assert skipped["status"] == "skipped"
    assert skipped["inputs"] is None
    assert skipped["outputs"] is None
    assert skipped["elapsed_ms"] == 0.0
    # skipped 记录排在已执行记录之后。
    statuses = [t["status"] for t in traces]
    assert statuses.index("skipped") > statuses.index("succeeded")
    assert all(s == "skipped" for s in statuses[statuses.index("skipped"):])


# ---- 失败：failed 记录 + 异常照抛 + 无 sweep --------------------------------


def test_failure_records_failed_and_reraises() -> None:
    ns = _exec(_gen(FAIL_YAML, emit_node_traces=True))
    with pytest.raises(ValueError, match="boom"):
        ns["run_workflow"](ns["Inputs"](question="q"), provider=MockProvider())
    traces = _traces(ns)
    by_id = {t["node_id"]: t for t in traces}
    assert by_id["code_1"]["status"] == "failed"
    assert "ValueError" in by_id["code_1"]["error"]
    assert by_id["code_1"]["outputs"] is None
    # 失败后未执行节点无记录（异常路径不 sweep）。
    assert "end_1" not in by_id
    assert all(t["status"] != "skipped" for t in traces)


# ---- qa_fold：kr 与 llm 两条记录，同计时同状态 -------------------------------


def test_qa_fold_emits_kr_and_llm_records(
    fixture_text: Callable[[str], str],
) -> None:
    ns = _exec(_gen(fixture_text("qa_fold"), emit_node_traces=True))
    ns["run_workflow"](ns["Inputs"](question="q"), provider=MockProvider())
    traces = _traces(ns)
    ids = [t["node_id"] for t in traces]
    assert ids.index("kr_1") + 1 == ids.index("llm_1")  # 先 kr 后 llm
    by_id = {t["node_id"]: t for t in traces}
    kr, llm = by_id["kr_1"], by_id["llm_1"]
    assert kr["node_type"] == "knowledge-retrieval"
    assert llm["node_type"] == "llm"
    assert kr["status"] == llm["status"] == "succeeded"
    assert kr["elapsed_ms"] == llm["elapsed_ms"]
    assert "result" in kr["outputs"]
    assert "text" in llm["outputs"]


# ---- iteration：单条记录，子图节点不逐项记 -----------------------------------


def test_iteration_is_single_record(fixture_text: Callable[[str], str]) -> None:
    ns = _exec(_gen(fixture_text("iteration"), emit_node_traces=True))
    ns["run_workflow"](ns["Inputs"](items=["a", "b"]), provider=MockProvider())
    traces = _traces(ns)
    ids = [t["node_id"] for t in traces]
    assert ids == ["start_1", "iter_1", "end_1"]
    assert "iter_llm" not in ids  # 子图内部节点不记
    by_id = {t["node_id"]: t for t in traces}
    assert by_id["iter_1"]["status"] == "succeeded"
    assert "output" in by_id["iter_1"]["outputs"]
