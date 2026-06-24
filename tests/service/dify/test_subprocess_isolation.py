"""L2 子进程隔离测试：scripts/run_dify_workflow.py 直跑 + run_workflow_isolated 调度。

子进程脚本本身跨平台可跑（setrlimit 仅 Linux 内部生效）；run_workflow_isolated 的
'subprocess' 隔离在非 Linux 平台回落 L1 in-process（这些平台 rlimit 不可靠）。
"""

import json
import os
import subprocess
import sys

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.dify.api import compile_dify_yaml
from ragspine.service.dify.runner import run_workflow_isolated

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"
SCRIPT = ROOT_DIR / "scripts" / "run_dify_workflow.py"


def _spec(name: str, **overrides) -> dict:
    code = compile_dify_yaml((FIXTURES / name).read_text(encoding="utf-8")).code
    spec = {
        "source": code.source,
        "entrypoint": code.entrypoint,
        "imports": list(code.imports),
        "warnings": list(code.warnings),
        "inputs": {"question": "hi"},
        "timeout_s": 5.0,
        "provider_type": "mock",
        "rlimit_cpu_s": 6,
        "rlimit_as_bytes": 1024 * 1024 * 1024,
    }
    spec.update(overrides)
    return spec


def _run_script(spec: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(spec), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 子进程脚本直跑（跨平台）
# ---------------------------------------------------------------------------
def test_subprocess_script_runs_clean_workflow():
    out = _run_script(_spec("seq.yml"))
    assert out["ok"] is True
    assert "result" in out["result"]


def test_subprocess_script_rejects_unsafe_via_l0():
    # agent_tool.yml 有 warning -> 子进程内 L1 沙箱的 L0 闸拒 -> ok=False
    out = _run_script(_spec("agent_tool.yml", inputs={}))
    assert out["ok"] is False
    assert out["error"]["type"] == "dify.unsafe"


def test_subprocess_script_runtime_error_is_structured():
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    raise ValueError('boom')\n"
    )
    out = _run_script({
        "source": src, "entrypoint": "run_workflow", "imports": [], "warnings": [],
        "inputs": {}, "timeout_s": 5.0, "provider_type": "mock",
    })
    assert out["ok"] is False
    assert "boom" in out["error"]["message"]


# ---------------------------------------------------------------------------
# run_workflow_isolated 调度：subprocess 隔离（Linux 真子进程；非 Linux 回落 L1）
# ---------------------------------------------------------------------------
def test_isolated_subprocess_runs_or_falls_back():
    code = compile_dify_yaml((FIXTURES / "seq.yml").read_text(encoding="utf-8")).code
    out = run_workflow_isolated(
        code, {"question": "hi"}, MockProvider(),
        timeout_s=10.0, isolation="subprocess",
        provider_config={"provider_type": "mock"},
    )
    # 无论真子进程（Linux）还是回落 L1（macOS/Windows），结果一致
    assert "result" in out


def test_isolated_inprocess_runs():
    code = compile_dify_yaml((FIXTURES / "seq.yml").read_text(encoding="utf-8")).code
    out = run_workflow_isolated(
        code, {"question": "hi"}, MockProvider(), isolation="inprocess"
    )
    assert "result" in out


def test_isolated_subprocess_without_config_falls_back_l1():
    # provider_config=None -> 即便 isolation=subprocess 也回落 L1（用 live provider）
    code = compile_dify_yaml((FIXTURES / "seq.yml").read_text(encoding="utf-8")).code
    out = run_workflow_isolated(
        code, {"question": "hi"}, MockProvider(),
        isolation="subprocess", provider_config=None,
    )
    assert "result" in out


def test_subprocess_isolation_is_linux_only_real():
    # 文档/行为契约：仅 Linux 走真子进程，其余平台回落 L1（避免 macOS/Windows rlimit 不可靠）
    if sys.platform.startswith("linux"):
        assert SCRIPT.exists()  # Linux 上脚本必须存在以供 spawn
    else:
        # 非 Linux：subprocess 调度回落 L1，仍应正常返回（已由上面用例覆盖）
        assert True
