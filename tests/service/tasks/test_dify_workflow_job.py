"""worker 端 run_dify_workflow_job 单元测试：自建 provider、防御式 L0 闸、JSON 结果。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.dify.api import compile_dify_yaml
from ragspine.service.tasks.jobs import run_dify_workflow_job
from ragspine.service.tasks.task_queue import JobError

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _payload(name: str, **overrides):
    code = compile_dify_yaml((FIXTURES / name).read_text(encoding="utf-8")).code
    payload = {
        "source": code.source,
        "entrypoint": code.entrypoint,
        "imports": list(code.imports),
        "warnings": list(code.warnings),
        "inputs": {"question": "hi"},
        "timeout_s": 10.0,
        "isolation": "inprocess",
        "provider_type": "mock",
    }
    payload.update(overrides)
    return payload


def test_job_runs_with_self_built_mock_provider():
    out = run_dify_workflow_job(_payload("seq.yml"))
    assert "result" in out
    assert "result" in out["result"]  # 工作流 _result dict
    assert isinstance(out["result"]["result"], str)
    assert out["warnings"] == []


def test_job_defensive_l0_gate_rejects_unsafe():
    # 即便入队方塞了带 warning 的代码，worker 防御式 L0 闸仍拒（JobError stage=validation）。
    payload = _payload("agent_tool.yml", inputs={})
    assert payload["warnings"]  # agent_tool 有 tool 占位 warning
    with pytest.raises(JobError) as exc:
        run_dify_workflow_job(payload)
    assert exc.value.stage == "validation"


def test_job_execution_error_is_joberror():
    # 注入一段会运行期抛错的合法-import 代码（绕过 L0），worker 整形为 JobError(execution)。
    src = (
        "from dataclasses import dataclass\n"
        "from typing import Any\n"
        "@dataclass\n"
        "class Inputs:\n"
        "    x: Any = None\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    raise ValueError('boom')\n"
    )
    payload = {
        "source": src, "entrypoint": "run_workflow", "imports": [], "warnings": [],
        "inputs": {}, "timeout_s": 10.0, "isolation": "inprocess", "provider_type": "mock",
    }
    with pytest.raises(JobError) as exc:
        run_dify_workflow_job(payload)
    assert exc.value.stage == "execution"
