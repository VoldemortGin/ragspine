"""服务层 /v1/n8n/* HTTP 行为测试（薄适配层，只验证外部行为）。

覆盖 convert（双向、dict/str 入参、错误整形 400/422）与 run（n8n→dify 后完整复用
dify run 管线：默认关 403、成功 200 带 convert_warnings/node_traces、未知节点 L0 闸 422、
转换错误 400）。注入 MockProvider + FakeQueue，零真实 LLM API（TestClient）。
"""

import json
import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.tasks.task_queue import FakeQueue

N8N_FIXTURES = ROOT_DIR / "tests" / "n8n" / "fixtures"
DIFY_FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _n8n_fixture(name: str) -> dict:
    return json.loads((N8N_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _dify_fixture_text(name: str) -> str:
    return (DIFY_FIXTURES / f"{name}.yml").read_text(encoding="utf-8")


def _make_client(tmp_path, *, run_enabled=False, provider=None):
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=run_enabled,
    )
    app = create_app(
        config, provider=provider or MockProvider(), queue=FakeQueue()
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return _make_client(tmp_path)


# ---------------------------------------------------------------------------
# CONVERT — n8n JSON ↔ Dify DSL 双向转换（纯转换，不执行，安全）
# ---------------------------------------------------------------------------
def test_n8n_convert_to_dify_returns_workflow_and_yaml(client):
    resp = client.post(
        "/v1/n8n/convert",
        json={"direction": "n8n_to_dify", "workflow": _n8n_fixture("linear")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    doc = body["workflow"]
    assert doc["app"]["mode"] == "workflow"
    types = [n["data"]["type"] for n in doc["workflow"]["graph"]["nodes"]]
    assert types == ["start", "llm", "template-transform", "end"]
    # yaml 给序列化好的 Dify DSL（safe_load 后与 workflow dict 一致）。
    import yaml

    assert yaml.safe_load(body["yaml"]) == doc
    # lmChat 并入 llm 的 model 配置 → 出 warning，绝不静默。
    assert any("lmChat" in w for w in body["warnings"])


def test_n8n_convert_accepts_json_string_workflow(client):
    text = (N8N_FIXTURES / "linear.json").read_text(encoding="utf-8")
    resp = client.post(
        "/v1/n8n/convert", json={"direction": "n8n_to_dify", "workflow": text}
    )
    assert resp.status_code == 200
    assert resp.json()["workflow"]["app"]["mode"] == "workflow"


def test_n8n_convert_to_n8n_from_dify_yaml(client):
    resp = client.post(
        "/v1/n8n/convert",
        json={"direction": "dify_to_n8n", "workflow": _dify_fixture_text("seq")},
    )
    assert resp.status_code == 200
    body = resp.json()
    workflow = body["workflow"]
    assert body["yaml"] is None  # to n8n 方向 yaml 为 None
    types = {n["type"] for n in workflow["nodes"]}
    assert "n8n-nodes-base.manualTrigger" in types
    assert "@n8n/n8n-nodes-langchain.agent" in types
    assert workflow["connections"]  # 以节点 name 为键的连接表
    # end 节点无 n8n 对应 → noOp + warning。
    assert "n8n-nodes-base.noOp" in types
    assert any("end" in w for w in body["warnings"])


def test_n8n_convert_invalid_workflow_is_400(client):
    resp = client.post(
        "/v1/n8n/convert",
        json={"direction": "n8n_to_dify", "workflow": {"foo": "bar"}},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "n8n.convert"
    assert err["request_id"]


def test_n8n_convert_bad_direction_is_422(client):
    resp = client.post(
        "/v1/n8n/convert",
        json={"direction": "sideways", "workflow": _n8n_fixture("linear")},
    )
    assert resp.status_code == 422  # pydantic Literal 校验


# ---------------------------------------------------------------------------
# RUN — n8n→dify 后完整复用 dify run 管线（同一信任边界开关）
# ---------------------------------------------------------------------------
def test_n8n_run_disabled_by_default(tmp_path):
    client = _make_client(tmp_path, run_enabled=False)
    resp = client.post(
        "/v1/n8n/run",
        json={"workflow": _n8n_fixture("linear"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "dify.run_disabled"


def test_n8n_run_linear_when_enabled(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/n8n/run",
        json={"workflow": _n8n_fixture("linear"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    # 合成 end 收集终端 set 节点输出（dify id = name 的 snake_case）。
    assert isinstance(body["result"]["format_output"], str)
    # 形状 = DifyRunResponse（warnings/node_traces）+ convert_warnings。
    assert body["warnings"] == []  # 全部节点已建模 → 零编译 warning
    assert any("lmChat" in w for w in body["convert_warnings"])
    traces = body["node_traces"]
    assert isinstance(traces, list)
    assert [t["node_id"] for t in traces] == [
        "when_clicking_execute_workflow", "ai_agent", "format_output", "end_1",
    ]


def test_n8n_run_provider_from_server_not_client(tmp_path):
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, tools=None):
            self.calls += 1
            return MockProvider().chat(messages, tools=tools)

    spy = SpyProvider()
    client = _make_client(tmp_path, run_enabled=True, provider=spy)
    resp = client.post(
        "/v1/n8n/run",
        json={"workflow": _n8n_fixture("linear"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 200
    assert spy.calls == 1  # linear 有一个 llm 节点 → 服务端 provider 被调用一次


def test_n8n_run_unknown_node_rejected_by_static_gate(tmp_path):
    # unknown.json 的 httpRequest → n8n-passthrough → 编译骨架 + warning → L0 闸 422。
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/n8n/run", json={"workflow": _n8n_fixture("unknown"), "inputs": {}}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "dify.unsafe"


def test_n8n_run_convert_error_is_400(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/n8n/run", json={"workflow": {"foo": "bar"}, "inputs": {}}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "n8n.convert"
