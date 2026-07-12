"""服务层 /v1/workflows/*（Dify 官方 Workflow App API 形状克隆）HTTP 行为测试。

验证对外形状与 Dify 官方 Workflow App API 一致（现有 dify SDK / 客户端零改动直连）：
Bearer app-key 鉴权（key -> 服务端注册的 workflow YAML）、blocking / streaming 两种
response_mode、官方错误体 {code, message, status}、run 摘要查询、/info + /parameters。
注入 MockProvider + FakeQueue，零真实 LLM API（TestClient）。
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

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"
SEQ_YML = FIXTURES / "seq.yml"

AUTH = {"Authorization": "Bearer app-key-1"}

# 会在 code 节点抛 ValueError 的 workflow（与 test_api_dify.py 的 FAIL_TRACE_YAML 同构）。
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


def _make_client(tmp_path, *, apps=None, run_enabled=True, provider=None):
    apps_str = apps if apps is not None else f"app-key-1={SEQ_YML}"
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=run_enabled,
        dify_public_apps=apps_str,
    )
    app = create_app(config, provider=provider or MockProvider(), queue=FakeQueue())
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return _make_client(tmp_path)


def _run_body(**overrides):
    body = {"inputs": {"question": "hi"}, "response_mode": "blocking", "user": "u-1"}
    body.update(overrides)
    return body


def _parse_sse(text: str) -> list:
    """逐块解析 SSE：每块 `data: {...}\\n\\n`。"""
    events = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        assert block.startswith("data: "), f"非法 SSE 块: {block!r}"
        events.append(json.loads(block[len("data: "):]))
    return events


# ---------------------------------------------------------------------------
# 鉴权 — Bearer app-key（官方 401 形状 {code: unauthorized, message, status}）
# ---------------------------------------------------------------------------
def test_run_without_auth_header_is_401(client):
    resp = client.post("/v1/workflows/run", json=_run_body())
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "unauthorized"
    assert body["status"] == 401
    assert body["message"]


def test_run_with_wrong_key_is_401(client):
    resp = client.post(
        "/v1/workflows/run", json=_run_body(),
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_run_with_non_bearer_scheme_is_401(client):
    resp = client.post(
        "/v1/workflows/run", json=_run_body(),
        headers={"Authorization": "Basic app-key-1"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_run_with_no_apps_configured_is_401(tmp_path):
    client = _make_client(tmp_path, apps="")
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# 参数校验 / 开关 — 官方 400 形状（invalid_param / app_unavailable）
# ---------------------------------------------------------------------------
def test_run_missing_user_is_400_invalid_param(client):
    resp = client.post(
        "/v1/workflows/run",
        json={"inputs": {"question": "hi"}, "response_mode": "blocking"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "invalid_param"
    assert body["status"] == 400


def test_run_bad_response_mode_is_400_invalid_param(client):
    resp = client.post(
        "/v1/workflows/run", json=_run_body(response_mode="nonsense"), headers=AUTH
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_param"


def test_run_disabled_is_400_app_unavailable(tmp_path):
    client = _make_client(tmp_path, run_enabled=False)
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "app_unavailable"
    assert body["status"] == 400


def test_run_registered_yaml_file_missing_is_400_app_unavailable(tmp_path):
    client = _make_client(tmp_path, apps=f"app-key-1={tmp_path / 'nope.yml'}")
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "app_unavailable"
    assert "nope.yml" in body["message"]


# ---------------------------------------------------------------------------
# BLOCKING — 官方 CompletionResponse 形状 {workflow_run_id, task_id, data{...}}
# ---------------------------------------------------------------------------
def test_blocking_run_success_shape(client):
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_run_id"]
    assert body["task_id"]
    data = body["data"]
    assert data["id"] == body["workflow_run_id"]
    assert data["workflow_id"]
    assert data["status"] == "succeeded"
    # outputs = 现有 run 管线的 result（seq.yml 的 end 节点输出 result 键）
    assert isinstance(data["outputs"]["result"], str)
    assert data["error"] is None
    assert isinstance(data["elapsed_time"], float) and data["elapsed_time"] >= 0.0
    assert data["total_tokens"] == 0
    assert data["total_steps"] == 4  # start_1 / llm_1 / tt_1 / end_1
    assert isinstance(data["created_at"], int)
    assert isinstance(data["finished_at"], int)
    assert data["finished_at"] >= data["created_at"]


def test_blocking_is_default_response_mode(client):
    body = {"inputs": {"question": "hi"}, "user": "u-1"}  # 不带 response_mode
    resp = client.post("/v1/workflows/run", json=body, headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["data"]["status"] == "succeeded"


def test_blocking_run_failure_is_200_with_status_failed(tmp_path):
    fail_path = tmp_path / "fail.yml"
    fail_path.write_text(FAIL_YAML, encoding="utf-8")
    client = _make_client(tmp_path, apps=f"app-key-1={fail_path}")
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    # 与 dify 行为一致：workflow 执行失败仍 200，data.status=failed + data.error
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert "ValueError" in data["error"]
    assert data["outputs"] is None
    assert data["total_steps"] >= 1  # start_1 已执行


def test_blocking_compile_error_is_200_with_status_failed(tmp_path):
    bad_path = tmp_path / "bad.yml"
    bad_path.write_text(": : bad : [", encoding="utf-8")
    client = _make_client(tmp_path, apps=f"app-key-1={bad_path}")
    resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["error"]
    assert data["total_steps"] == 0


def test_multiple_apps_selected_by_key(tmp_path):
    fail_path = tmp_path / "fail.yml"
    fail_path.write_text(FAIL_YAML, encoding="utf-8")
    client = _make_client(
        tmp_path, apps=f"app-key-1={SEQ_YML};app-key-2={fail_path}"
    )
    ok = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert ok.json()["data"]["status"] == "succeeded"
    bad = client.post(
        "/v1/workflows/run", json=_run_body(),
        headers={"Authorization": "Bearer app-key-2"},
    )
    assert bad.json()["data"]["status"] == "failed"


# ---------------------------------------------------------------------------
# STREAMING — SSE 回放：workflow_started → (node_started/node_finished)* →
# workflow_finished（skipped 节点不发事件）
# ---------------------------------------------------------------------------
def test_streaming_run_event_sequence(client):
    resp = client.post(
        "/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)

    kinds = [e["event"] for e in events]
    assert kinds == (
        ["workflow_started"]
        + ["node_started", "node_finished"] * 4
        + ["workflow_finished"]
    )
    # 所有事件共享同一 workflow_run_id / task_id
    run_ids = {e["workflow_run_id"] for e in events}
    task_ids = {e["task_id"] for e in events}
    assert len(run_ids) == 1 and len(task_ids) == 1

    started = events[0]
    assert started["data"]["id"] == events[0]["workflow_run_id"]
    assert started["data"]["workflow_id"]
    assert isinstance(started["data"]["created_at"], int)

    node_started = [e for e in events if e["event"] == "node_started"]
    node_finished = [e for e in events if e["event"] == "node_finished"]
    assert [e["data"]["node_id"] for e in node_started] == [
        "start_1", "llm_1", "tt_1", "end_1"
    ]
    assert [e["data"]["index"] for e in node_started] == [1, 2, 3, 4]

    llm = node_finished[1]["data"]
    assert llm["node_id"] == "llm_1"
    assert llm["node_type"] == "llm"
    assert llm["title"] == "应答模型"
    assert llm["index"] == 2
    assert llm["status"] == "succeeded"
    assert llm["error"] is None
    assert isinstance(llm["elapsed_time"], float) and llm["elapsed_time"] >= 0.0
    assert isinstance(llm["outputs"], dict) and "text" in llm["outputs"]
    assert llm["predecessor_node_id"] == "start_1"

    finished = events[-1]["data"]
    assert finished["status"] == "succeeded"
    assert isinstance(finished["outputs"]["result"], str)
    assert finished["error"] is None
    assert finished["total_steps"] == 4
    assert finished["total_tokens"] == 0
    assert isinstance(finished["elapsed_time"], float)
    assert isinstance(finished["finished_at"], int)


def test_streaming_run_failure_replays_failed_node(tmp_path):
    fail_path = tmp_path / "fail.yml"
    fail_path.write_text(FAIL_YAML, encoding="utf-8")
    client = _make_client(tmp_path, apps=f"app-key-1={fail_path}")
    resp = client.post(
        "/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["event"] == "workflow_started"
    assert events[-1]["event"] == "workflow_finished"
    assert events[-1]["data"]["status"] == "failed"
    assert "ValueError" in events[-1]["data"]["error"]
    failed = [
        e for e in events
        if e["event"] == "node_finished" and e["data"]["status"] == "failed"
    ]
    assert failed and failed[0]["data"]["node_id"] == "code_1"
    assert "ValueError" in failed[0]["data"]["error"]
    # skipped 节点（end_1 未执行）不发事件
    assert "end_1" not in [
        e["data"]["node_id"] for e in events if e["event"] == "node_started"
    ]


# ---------------------------------------------------------------------------
# GET /v1/workflows/run/{id} — run 摘要查询（进程内 LRU，官方响应形状）
# ---------------------------------------------------------------------------
def test_get_run_detail_after_blocking_run(client):
    run = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    run_id = run.json()["workflow_run_id"]

    resp = client.get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == run_id
    assert body["workflow_id"] == run.json()["data"]["workflow_id"]
    assert body["status"] == "succeeded"
    assert body["inputs"] == {"question": "hi"}
    assert isinstance(body["outputs"]["result"], str)
    assert body["error"] is None
    assert body["total_steps"] == 4
    assert body["total_tokens"] == 0
    assert isinstance(body["created_at"], int)
    assert isinstance(body["finished_at"], int)
    assert isinstance(body["elapsed_time"], float)


def test_get_run_detail_unknown_id_is_404(client):
    resp = client.get("/v1/workflows/run/no-such-run", headers=AUTH)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"
    assert body["status"] == 404


def test_get_run_detail_requires_auth(client):
    resp = client.get("/v1/workflows/run/whatever")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_get_run_detail_scoped_to_app_key(tmp_path):
    fail_path = tmp_path / "fail.yml"
    fail_path.write_text(FAIL_YAML, encoding="utf-8")
    client = _make_client(
        tmp_path, apps=f"app-key-1={SEQ_YML};app-key-2={fail_path}"
    )
    run = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    run_id = run.json()["workflow_run_id"]
    # 另一个 app 的 key 查不到这个 run（与 dify 一 key 一 app 语义一致）
    resp = client.get(
        f"/v1/workflows/run/{run_id}",
        headers={"Authorization": "Bearer app-key-2"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_run_store_evicts_oldest_beyond_capacity(client, monkeypatch):
    import ragspine.service.api.dify_public as dify_public

    monkeypatch.setattr(dify_public, "_MAX_RUNS", 2)
    ids = [
        client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
        .json()["workflow_run_id"]
        for _ in range(3)
    ]
    assert client.get(f"/v1/workflows/run/{ids[0]}", headers=AUTH).status_code == 404
    assert client.get(f"/v1/workflows/run/{ids[1]}", headers=AUTH).status_code == 200
    assert client.get(f"/v1/workflows/run/{ids[2]}", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# GET /v1/info / /v1/parameters — dify SDK 会调的应用元信息端点
# ---------------------------------------------------------------------------
def test_info_shape(client):
    resp = client.get("/v1/info", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "seq-demo"
    assert "description" in body
    assert body["tags"] == []
    assert body["mode"] == "workflow"


def test_info_requires_auth(client):
    resp = client.get("/v1/info")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_parameters_derives_user_input_form_from_start_node(client):
    resp = client.get("/v1/parameters", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_input_form"] == [
        {
            "text-input": {
                "label": "问题",
                "variable": "question",
                "required": True,
                "default": "",
            }
        }
    ]
    assert "file_upload" in body
    assert "system_parameters" in body
