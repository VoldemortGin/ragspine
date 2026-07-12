"""L1 受限 exec runner 单元测试：__build_class__ 修复、受限 builtins、超时、隔离。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.dify.runner import (
    DifyRunError,
    DifyTimeoutError,
    run_generated,
)
from ragspine.service.dify.safety import DifyUnsafeError

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _code(name: str) -> GeneratedCode:
    return compile_dify_yaml((FIXTURES / name).read_text(encoding="utf-8")).code


# ---------------------------------------------------------------------------
# __build_class__ 修复：生成代码顶部 @dataclass class Inputs 在受限 builtins 下能跑
# ---------------------------------------------------------------------------
def test_runs_dataclass_inputs_under_restricted_builtins():
    # seq.yml 生成 @dataclass class Inputs + from __future__ import annotations；
    # 受限 builtins 必须放行 __build_class__ 且把模块注册进 sys.modules，否则 exec 失败。
    out = run_generated(_code("seq.yml"), {"question": "hello"}, MockProvider())
    assert "result" in out
    assert isinstance(out["result"], str)


def test_runs_all_clean_fixtures():
    for name, kw in [
        ("branch.yml", {"question": "q"}),
        ("parallel.yml", {"question": "q"}),
        ("iteration.yml", {"items": ["a", "b"]}),
        ("knowledge.yml", {"question": "q"}),
        ("qa_fold.yml", {"question": "q"}),
    ]:
        out = run_generated(_code(name), kw, MockProvider())
        assert isinstance(out, dict) and out  # 非空结果 dict


def test_extra_inputs_ignored():
    # 客户端传多余字段，只取 Inputs 声明的字段，不报错
    out = run_generated(
        _code("seq.yml"), {"question": "x", "bogus": 1, "evil": "rm -rf"}, MockProvider()
    )
    assert "result" in out


# ---------------------------------------------------------------------------
# L0 闸先行：unsafe 代码在任何 exec 之前被拒
# ---------------------------------------------------------------------------
def test_unsafe_rejected_before_exec():
    with pytest.raises(DifyUnsafeError):
        run_generated(_code("agent_tool.yml"), {}, MockProvider())


# ---------------------------------------------------------------------------
# 受限 builtins：open / eval / __import__('os') 不可达
# ---------------------------------------------------------------------------
def test_open_not_available_in_sandbox():
    # 构造一段「合法 import 白名单、但运行期试图 open 文件」的代码。
    # L0 闸只看 import（这段不 import os），故能进 exec；exec 内 open 应 NameError。
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    open('/etc/passwd')\n"
        "    return {}\n"
    )
    with pytest.raises(DifyRunError) as exc:
        run_generated(GeneratedCode(source=src), {}, MockProvider())
    assert "open" in str(exc.value) or "NameError" in str(exc.value)


def test_dynamic_import_os_blocked():
    # 即便绕过 L0（不在顶部静态 import），运行期 __import__('os') 也被受限 __import__ 挡掉。
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    __import__('os').system('echo pwned')\n"
        "    return {}\n"
    )
    with pytest.raises(DifyRunError) as exc:
        run_generated(GeneratedCode(source=src), {}, MockProvider())
    # 受限 __import__ 抛 ImportError -> 整形为 DifyRunError
    assert "os" in str(exc.value) or "import" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# 超时：长时间执行触发 DifyTimeoutError（跨平台线程软超时）
# ---------------------------------------------------------------------------
def test_timeout_raises():
    # range/sum 是白名单 builtin；一个大循环跑满超时窗口即触发软超时。
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    s = 0\n"
        "    for _ in range(10**12):\n"
        "        s += 1\n"
        "    return {'s': s}\n"
    )
    with pytest.raises(DifyTimeoutError) as exc:
        run_generated(GeneratedCode(source=src), {}, MockProvider(), timeout_s=0.3)
    assert exc.value.code == "dify.timeout"


# ---------------------------------------------------------------------------
# 运行期异常整形：生成代码抛普通异常 -> DifyRunError（不外泄 traceback 对象）
# ---------------------------------------------------------------------------
def test_runtime_error_wrapped():
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    raise ValueError('boom')\n"
    )
    with pytest.raises(DifyRunError) as exc:
        run_generated(GeneratedCode(source=src), {}, MockProvider())
    assert exc.value.code == "dify.run_error"
    assert "boom" in str(exc.value)


# ---------------------------------------------------------------------------
# node trace 采集：emit_node_traces=True 编译的代码 -> __node_traces__ / context 附着
# ---------------------------------------------------------------------------
FAIL_TRACE_YAML = """
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
            - {variable: question, label: 问题, type: text-input, required: true}
      - id: code_1
        data:
          type: code
          title: 会炸的代码
          code: "def main(x):\\n    raise ValueError('boom')\\n"
          code_language: python3
          variables:
            - {variable: x, value_selector: [start_1, question]}
          outputs:
            out: {type: string}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {variable: out, value_selector: [code_1, out]}
    edges:
      - {source: start_1, target: code_1, sourceHandle: source}
      - {source: code_1, target: end_1, sourceHandle: source}
"""


def _traced_code(name: str) -> GeneratedCode:
    return compile_dify_yaml(
        (FIXTURES / name).read_text(encoding="utf-8"), emit_node_traces=True
    ).code


def test_run_generated_returns_sanitized_node_traces():
    out = run_generated(_traced_code("seq.yml"), {"question": "hello"}, MockProvider())
    assert "result" in out
    traces = out["__node_traces__"]
    assert isinstance(traces, list) and traces
    assert [t["index"] for t in traces] == list(range(len(traces)))
    for t in traces:
        assert t["status"] == "succeeded"
        assert isinstance(t["elapsed_ms"], float)
        assert t["elapsed_ms"] >= 0.0
    # runner 注入了 _TRACE_CLOCK（time.perf_counter）：llm 节点计时应为正。
    llm = next(t for t in traces if t["node_id"] == "llm_1")
    assert llm["elapsed_ms"] > 0.0


def test_run_generated_failure_attaches_traces_to_context():
    code = compile_dify_yaml(FAIL_TRACE_YAML, emit_node_traces=True).code
    with pytest.raises(DifyRunError) as exc:
        run_generated(code, {"question": "q"}, MockProvider())
    assert exc.value.code == "dify.run_error"
    assert "boom" in str(exc.value)
    traces = exc.value.context["node_traces"]
    assert isinstance(traces, list) and traces
    failed = [t for t in traces if t["status"] == "failed"]
    assert failed and failed[0]["node_id"] == "code_1"
    assert "ValueError" in failed[0]["error"]


def test_run_generated_without_trace_flag_is_backward_compatible():
    out = run_generated(_code("seq.yml"), {"question": "hello"}, MockProvider())
    assert "__node_traces__" not in out
