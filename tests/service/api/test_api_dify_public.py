"""服务层 /v1/workflows/*（Dify 官方 Workflow App API 形状克隆）HTTP 行为测试。

验证对外形状与 Dify 官方 Workflow App API 一致（现有 dify SDK / 客户端零改动直连）：
Bearer app-key 鉴权（key -> 服务端注册的 workflow YAML）、blocking / streaming 两种
response_mode、官方错误体 {code, message, status}、run 摘要查询、/info + /parameters。
注入 MockProvider + FakeQueue，零真实 LLM API（TestClient）。
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

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
        events.append(json.loads(block[len("data: ") :]))
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
        "/v1/workflows/run",
        json=_run_body(),
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_run_with_non_bearer_scheme_is_401(client):
    resp = client.post(
        "/v1/workflows/run",
        json=_run_body(),
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
    resp = client.post("/v1/workflows/run", json=_run_body(response_mode="nonsense"), headers=AUTH)
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
    client = _make_client(tmp_path, apps=f"app-key-1={SEQ_YML};app-key-2={fail_path}")
    ok = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert ok.json()["data"]["status"] == "succeeded"
    bad = client.post(
        "/v1/workflows/run",
        json=_run_body(),
        headers={"Authorization": "Bearer app-key-2"},
    )
    assert bad.json()["data"]["status"] == "failed"


# ---------------------------------------------------------------------------
# STREAMING — SSE 回放：workflow_started → (node_started/node_finished)* →
# workflow_finished（skipped 节点不发事件）
# ---------------------------------------------------------------------------
def test_streaming_run_event_sequence(client):
    resp = client.post("/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)

    kinds = [e["event"] for e in events]
    assert kinds == (
        ["workflow_started"] + ["node_started", "node_finished"] * 4 + ["workflow_finished"]
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
    assert [e["data"]["node_id"] for e in node_started] == ["start_1", "llm_1", "tt_1", "end_1"]
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
    resp = client.post("/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH)
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["event"] == "workflow_started"
    assert events[-1]["event"] == "workflow_finished"
    assert events[-1]["data"]["status"] == "failed"
    assert "ValueError" in events[-1]["data"]["error"]
    failed = [
        e for e in events if e["event"] == "node_finished" and e["data"]["status"] == "failed"
    ]
    assert failed and failed[0]["data"]["node_id"] == "code_1"
    assert "ValueError" in failed[0]["data"]["error"]
    # skipped 节点（end_1 未执行）不发事件
    assert "end_1" not in [e["data"]["node_id"] for e in events if e["event"] == "node_started"]


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
    client = _make_client(tmp_path, apps=f"app-key-1={SEQ_YML};app-key-2={fail_path}")
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
        client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()["workflow_run_id"]
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


# ===========================================================================
# S1 可选 SQLite 运行历史（PRD FR-100～FR-109 / 测试计划 HIST-001～HIST-018 / ADR 0026）
#
# 配置一律经 ServiceConfig.from_env 注入 RAGSPINE_DIFY_PUBLIC_RUN_STORE_PATH：未实现时
# 旧代码忽略该键仍可执行，红灯表现为「重建 app 后查询 404」等行为失败，而非构造器 TypeError。
# ===========================================================================
AUTH_2 = {"Authorization": "Bearer app-key-2"}
STORE_ENV = "RAGSPINE_DIFY_PUBLIC_RUN_STORE_PATH"
HISTORY_UNAVAILABLE = "history_unavailable"
# 详情响应的全部公开字段（FR-101：重启后必须原样保留）。
DETAIL_FIELDS = (
    "id",
    "workflow_id",
    "status",
    "inputs",
    "outputs",
    "error",
    "total_steps",
    "total_tokens",
    "created_at",
    "finished_at",
    "elapsed_time",
)


def _store_config(tmp_path, store_path, *, apps=None, run_enabled=True) -> ServiceConfig:
    env = {
        "RAGSPINE_DB_PATH": str(tmp_path / "fact.db"),
        "RAGSPINE_DIFY_PUBLIC_APPS": apps if apps is not None else f"app-key-1={SEQ_YML}",
        STORE_ENV: str(store_path),
    }
    if run_enabled:
        env["RAGSPINE_DIFY_RUN_ENABLED"] = "true"
    return ServiceConfig.from_env(env)


def _make_store_client(tmp_path, store_path, *, apps=None, run_enabled=True, provider=None):
    """配置 SQLite 历史路径的独立 app（每次调用都是全新 app / 全新 app.state）。"""
    config = _store_config(tmp_path, store_path, apps=apps, run_enabled=run_enabled)
    app = create_app(config, provider=provider or MockProvider(), queue=FakeQueue())
    return TestClient(app)


# 高辨识业务正文：用来证明 503 错误体 / 日志不回显请求正文。
BODY_SECRET = "正文-HIST-SECRET-5e8d2b"


def _secret_body(**overrides):
    return _run_body(inputs={"question": BODY_SECRET}, **overrides)


def _fail_yaml(tmp_path):
    path = tmp_path / "fail.yml"
    path.write_text(FAIL_YAML, encoding="utf-8")
    return path


def _bad_yaml(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text(": : bad : [", encoding="utf-8")
    return path


def _assert_history_unavailable(resp, *secrets: str) -> None:
    """脱敏的 503 / history_unavailable；错误体不含路径、key 或业务正文。"""
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["code"] == HISTORY_UNAVAILABLE
    assert body["status"] == 503
    assert body["message"]
    for secret in secrets:
        assert secret not in resp.text


def _count_executions(monkeypatch) -> list:
    """包一层 _execute_workflow 计数（证明读取 / 存储失败不再次执行工作流）。"""
    import ragspine.service.api.dify_public as dify_public

    calls: list = []
    real = dify_public._execute_workflow

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(dify_public, "_execute_workflow", _counting)
    return calls


def _history_files(store_path) -> list:
    """数据库本体及可能存在的 -wal / -shm / -journal 旁路文件。"""
    return [
        p for p in store_path.parent.iterdir() if p.is_file() and p.name.startswith(store_path.name)
    ]


class _FailingCommitConnection(sqlite3.Connection):
    """注入提交错误：显式 COMMIT 或 commit() 一律抛 OperationalError。"""

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith(("COMMIT", "END")):
            raise sqlite3.OperationalError("injected commit failure")
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        raise sqlite3.OperationalError("injected commit failure")


def _inject_commit_failure(monkeypatch) -> None:
    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        kwargs["factory"] = _FailingCommitConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _connect)


# ---------------------------------------------------------------------------
# HIST-001（FR-100）默认内存：不配置路径时行为不变、不落任何历史库
# ---------------------------------------------------------------------------
def test_hist_001_default_memory_history_is_per_app_and_writes_no_file(tmp_path):
    assert ServiceConfig(db_path="x.db").dify_public_run_store_path is None
    assert ServiceConfig.from_env({}).dify_public_run_store_path is None

    app_a = _make_client(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    run_id = app_a.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert app_a.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200

    app_b = _make_client(tmp_path)
    resp = app_b.get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    # 默认路径不增加任何持久化副作用（不创建历史数据库）。
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_hist_001_env_key_populates_config(tmp_path):
    store = tmp_path / "runs.db"
    config = _store_config(tmp_path, store)
    assert config.dify_public_run_store_path == str(store)


def test_hist_001_default_memory_keeps_global_capacity_across_keys(tmp_path, monkeypatch):
    """内存默认保持「全局最近 _MAX_RUNS 条」旧语义：另一 key 的写入也会淘汰本 key。"""
    import ragspine.service.api.dify_public as dify_public

    monkeypatch.setattr(dify_public, "_MAX_RUNS", 2)
    fail_path = _fail_yaml(tmp_path)
    client = _make_client(tmp_path, apps=f"app-key-1={SEQ_YML};app-key-2={fail_path}")
    a1 = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()["workflow_run_id"]
    b1 = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH_2).json()[
        "workflow_run_id"
    ]
    a2 = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()["workflow_run_id"]
    assert client.get(f"/v1/workflows/run/{a1}", headers=AUTH).status_code == 404
    assert client.get(f"/v1/workflows/run/{b1}", headers=AUTH_2).status_code == 200
    assert client.get(f"/v1/workflows/run/{a2}", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# HIST-002（FR-101）成功 blocking run 重建 app 后原样可查，读取不再执行
# ---------------------------------------------------------------------------
def test_hist_002_blocking_success_survives_new_app(tmp_path, monkeypatch):
    store = tmp_path / "runs.db"
    with _make_store_client(tmp_path, store) as app_a:
        run = app_a.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
        assert run.status_code == 200
        run_body = run.json()
        run_id = run_body["workflow_run_id"]
        detail_a = app_a.get(f"/v1/workflows/run/{run_id}", headers=AUTH).json()

    calls = _count_executions(monkeypatch)
    app_b = _make_store_client(tmp_path, store)
    resp = app_b.get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    detail_b = resp.json()
    assert calls == []  # 读取不再次调用 runner / provider
    assert {k: detail_b[k] for k in DETAIL_FIELDS} == {k: detail_a[k] for k in DETAIL_FIELDS}
    data = run_body["data"]
    assert detail_b["id"] == run_id
    assert detail_b["workflow_id"] == data["workflow_id"]
    assert detail_b["status"] == "succeeded"
    assert detail_b["inputs"] == {"question": "hi"}
    assert detail_b["outputs"] == data["outputs"]
    assert detail_b["error"] is None
    assert detail_b["total_steps"] == data["total_steps"] == 4
    assert detail_b["total_tokens"] == 0
    assert detail_b["created_at"] == data["created_at"]
    assert detail_b["finished_at"] == data["finished_at"]
    assert detail_b["elapsed_time"] == data["elapsed_time"]


# ---------------------------------------------------------------------------
# HIST-003（FR-101）真正的独立 Python 子进程写入、另一子进程读取
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = r"""
import json
import sys

from fastapi.testclient import TestClient

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.tasks.task_queue import FakeQueue

mode = sys.argv[1]
client = TestClient(
    create_app(ServiceConfig.from_env(), provider=MockProvider(), queue=FakeQueue())
)
headers = {"Authorization": "Bearer app-key-1"}
if mode == "write":
    resp = client.post(
        "/v1/workflows/run",
        json={"inputs": {"question": "跨进程"}, "response_mode": "blocking", "user": "u-1"},
        headers=headers,
    )
else:
    resp = client.get("/v1/workflows/run/" + sys.argv[2], headers=headers)
print("RESULT " + json.dumps({"status": resp.status_code, "body": resp.json()}))
"""


def _run_child(tmp_path, store, *args: str) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "RAGSPINE_DB_PATH": str(tmp_path / "fact.db"),
            "RAGSPINE_DIFY_PUBLIC_APPS": f"app-key-1={SEQ_YML}",
            "RAGSPINE_DIFY_RUN_ENABLED": "true",
            "RAGSPINE_PROVIDER_TYPE": "mock",
            STORE_ENV: str(store),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, *args],
        cwd=str(ROOT_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, proc.stdout[-2000:]
    return json.loads(lines[-1][len("RESULT ") :])


def test_hist_003_history_survives_separate_python_processes(tmp_path):
    store = tmp_path / "runs.db"
    written = _run_child(tmp_path, store, "write")
    assert written["status"] == 200
    run_id = written["body"]["workflow_run_id"]
    data = written["body"]["data"]

    read = _run_child(tmp_path, store, "read", run_id)
    assert read["status"] == 200, read
    detail = read["body"]
    assert detail["id"] == run_id
    assert detail["workflow_id"] == data["workflow_id"]
    assert detail["status"] == "succeeded"
    assert detail["inputs"] == {"question": "跨进程"}
    assert detail["outputs"] == data["outputs"]
    assert detail["total_steps"] == data["total_steps"]
    assert detail["created_at"] == data["created_at"]
    assert detail["finished_at"] == data["finished_at"]
    assert detail["elapsed_time"] == data["elapsed_time"]

    # 父进程（第三个进程）用全新 app 读同一文件也一致。
    resp = _make_store_client(tmp_path, store).get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == detail


# ---------------------------------------------------------------------------
# HIST-004（FR-102）编译失败与执行失败同样持久化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["compile_error", "runtime_error"])
def test_hist_004_failed_runs_survive_new_app(tmp_path, kind):
    yaml_path = _bad_yaml(tmp_path) if kind == "compile_error" else _fail_yaml(tmp_path)
    apps = f"app-key-1={yaml_path}"
    store = tmp_path / "runs.db"
    run = _make_store_client(tmp_path, store, apps=apps).post(
        "/v1/workflows/run", json=_run_body(), headers=AUTH
    )
    assert run.status_code == 200
    data = run.json()["data"]
    assert data["status"] == "failed"

    resp = _make_store_client(tmp_path, store, apps=apps).get(
        f"/v1/workflows/run/{data['id']}", headers=AUTH
    )
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["status"] == "failed"
    assert detail["error"] == data["error"]
    assert detail["outputs"] is None
    assert detail["total_steps"] == data["total_steps"]
    if kind == "compile_error":
        assert detail["total_steps"] == 0
    else:
        assert "ValueError" in detail["error"]
        assert detail["total_steps"] >= 1


# ---------------------------------------------------------------------------
# HIST-005（FR-102 / FR-109）streaming 回放的同一 run 可在新 app 读取
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["success", "failure"])
def test_hist_005_streaming_run_survives_new_app(tmp_path, kind):
    apps = f"app-key-1={SEQ_YML}" if kind == "success" else f"app-key-1={_fail_yaml(tmp_path)}"
    store = tmp_path / "runs.db"
    resp = _make_store_client(tmp_path, store, apps=apps).post(
        "/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    kinds = [e["event"] for e in events]
    # 原有回放规则不变：started 开头、finished 结尾、node 事件成对。
    assert kinds[0] == "workflow_started" and kinds[-1] == "workflow_finished"
    assert kinds[1:-1] == ["node_started", "node_finished"] * ((len(kinds) - 2) // 2)
    if kind == "success":
        assert len(kinds) == 2 + 2 * 4
    else:
        node_ids = [e["data"]["node_id"] for e in events if e["event"] == "node_started"]
        assert "end_1" not in node_ids  # skipped 节点仍不发事件
    finished = events[-1]["data"]
    run_id = events[-1]["workflow_run_id"]

    detail_resp = _make_store_client(tmp_path, store, apps=apps).get(
        f"/v1/workflows/run/{run_id}", headers=AUTH
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    for field in (
        "id",
        "workflow_id",
        "status",
        "outputs",
        "error",
        "total_steps",
        "total_tokens",
        "created_at",
        "finished_at",
        "elapsed_time",
    ):
        assert detail[field] == finished[field], field
    assert detail["status"] == ("succeeded" if kind == "success" else "failed")
    assert detail["inputs"] == {"question": "hi"}


# ---------------------------------------------------------------------------
# HIST-006（FR-103）app-key 归属隔离：同 YAML 不共享；重启后仍隔离；key 轮换不继承
# ---------------------------------------------------------------------------
def test_hist_006_ownership_is_by_api_key_not_yaml(tmp_path):
    store = tmp_path / "runs.db"
    apps = f"app-key-1={SEQ_YML};app-key-2={SEQ_YML}"  # 两个 key 注册同一 YAML
    client = _make_store_client(tmp_path, store, apps=apps)
    run_id = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert client.get(f"/v1/workflows/run/{run_id}", headers=AUTH_2).status_code == 404

    restarted = _make_store_client(tmp_path, store, apps=apps)
    assert restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
    other = restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH_2)
    assert other.status_code == 404
    assert other.json()["code"] == "not_found"
    # 原始 key 缺失 / 错误仍然 401。
    assert restarted.get(f"/v1/workflows/run/{run_id}").status_code == 401
    wrong = restarted.get(
        f"/v1/workflows/run/{run_id}", headers={"Authorization": "Bearer wrong-key"}
    )
    assert wrong.status_code == 401


def test_hist_006_key_rotation_does_not_inherit_history(tmp_path):
    store = tmp_path / "runs.db"
    old = _make_store_client(tmp_path, store, apps=f"app-key-1={SEQ_YML}")
    run_id = old.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()["workflow_run_id"]
    assert (
        _make_store_client(tmp_path, store, apps=f"app-key-1={SEQ_YML}")
        .get(f"/v1/workflows/run/{run_id}", headers=AUTH)
        .status_code
        == 200
    )

    # 同一 YAML、同一存储路径，换成新 key：旧历史不可查；旧 key 已不在注册表 -> 401。
    rotated = _make_store_client(tmp_path, store, apps=f"app-key-rotated={SEQ_YML}")
    resp = rotated.get(
        f"/v1/workflows/run/{run_id}", headers={"Authorization": "Bearer app-key-rotated"}
    )
    assert resp.status_code == 404
    assert rotated.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 401


# ---------------------------------------------------------------------------
# HIST-007（FR-103）未知 ID 与他人 ID 的 404 完全一致，不泄露存在性
# ---------------------------------------------------------------------------
def test_hist_007_unknown_and_foreign_ids_are_indistinguishable(tmp_path):
    store = tmp_path / "runs.db"
    apps = f"app-key-1={SEQ_YML};app-key-2={SEQ_YML}"
    client = _make_store_client(tmp_path, store, apps=apps)
    run_id = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    restarted = _make_store_client(tmp_path, store, apps=apps)
    assert restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200

    foreign = restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH_2)
    unknown = restarted.get(
        "/v1/workflows/run/00000000-0000-4000-8000-000000000000", headers=AUTH_2
    )
    assert foreign.status_code == unknown.status_code == 404
    assert (
        foreign.json()
        == unknown.json()
        == {
            "code": "not_found",
            "message": "Workflow run not found",
            "status": 404,
        }
    )


# ---------------------------------------------------------------------------
# HIST-008（FR-103 / FR-107）原始 bearer key 不落库（含 -wal/-shm/-journal）
# ---------------------------------------------------------------------------
def test_hist_008_raw_bearer_key_never_persisted(tmp_path, caplog):
    sentinel = "ragspine-HIST008-sentinel-9f3c7a1e5b2d4c6e8a0f"
    apps = f"{sentinel}={SEQ_YML}"
    headers = {"Authorization": f"Bearer {sentinel}"}
    store = tmp_path / "runs.db"
    client = _make_store_client(tmp_path, store, apps=apps)
    blocking_id = client.post("/v1/workflows/run", json=_run_body(), headers=headers).json()[
        "workflow_run_id"
    ]
    stream = client.post(
        "/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=headers
    )
    streaming_id = _parse_sse(stream.text)[-1]["workflow_run_id"]

    # 同 key 跨重启归属稳定（单向摘要是确定性的）。
    restarted = _make_store_client(tmp_path, store, apps=apps)
    for run_id in (blocking_id, streaming_id):
        assert restarted.get(f"/v1/workflows/run/{run_id}", headers=headers).status_code == 200

    files = _history_files(store)
    assert store in files
    raw = sentinel.encode("utf-8")
    for path in files:
        assert raw not in path.read_bytes(), path.name
    assert sentinel not in caplog.text


# ---------------------------------------------------------------------------
# HIST-009（FR-104）每 owner 容量：按写入顺序淘汰、读取不提升、跨重启有界
# ---------------------------------------------------------------------------
def test_hist_009_per_owner_capacity_is_fifo_and_survives_restart(tmp_path, monkeypatch):
    import ragspine.service.api.dify_public as dify_public

    # 每 owner 容量是可注入参数（默认 100）；此处缩为 2。
    monkeypatch.setattr(dify_public, "_MAX_RUNS_PER_OWNER", 2, raising=False)
    store = tmp_path / "runs.db"
    client = _make_store_client(tmp_path, store)

    def _post() -> str:
        return client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
            "workflow_run_id"
        ]

    run_a, run_b = _post(), _post()
    assert client.get(f"/v1/workflows/run/{run_a}", headers=AUTH).status_code == 200  # 读 A
    run_c = _post()

    restarted = _make_store_client(tmp_path, store)
    assert restarted.get(f"/v1/workflows/run/{run_b}", headers=AUTH).status_code == 200
    assert restarted.get(f"/v1/workflows/run/{run_c}", headers=AUTH).status_code == 200
    assert restarted.get(f"/v1/workflows/run/{run_a}", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# HIST-010（FR-104）一个 owner 的写入不能驱逐另一个 owner 的记录
# ---------------------------------------------------------------------------
def test_hist_010_owner_capacity_is_isolated(tmp_path, monkeypatch):
    import ragspine.service.api.dify_public as dify_public

    monkeypatch.setattr(dify_public, "_MAX_RUNS_PER_OWNER", 2, raising=False)
    store = tmp_path / "runs.db"
    apps = f"app-key-1={SEQ_YML};app-key-2={_fail_yaml(tmp_path)}"
    client = _make_store_client(tmp_path, store, apps=apps)

    def _post(headers) -> str:
        return client.post("/v1/workflows/run", json=_run_body(), headers=headers).json()[
            "workflow_run_id"
        ]

    a1, a2 = _post(AUTH), _post(AUTH)
    b1, b2 = _post(AUTH_2), _post(AUTH_2)
    a3 = _post(AUTH)

    restarted = _make_store_client(tmp_path, store, apps=apps)
    assert restarted.get(f"/v1/workflows/run/{a1}", headers=AUTH).status_code == 404
    for run_id in (a2, a3):
        assert restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
    for run_id in (b1, b2):
        assert restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH_2).status_code == 200
        # 归属隔离依旧成立。
        assert restarted.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# HIST-011（FR-007 / FR-108）空格 + 中文目录与文件名
# ---------------------------------------------------------------------------
def test_hist_011_unicode_and_space_path(tmp_path):
    folder = tmp_path / "运行 历史 目录"
    folder.mkdir()
    store = folder / "历史 记录.db"
    with _make_store_client(tmp_path, store) as client:
        run_id = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
            "workflow_run_id"
        ]
    assert store.is_file()
    resp = _make_store_client(tmp_path, store).get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["id"] == run_id


# ---------------------------------------------------------------------------
# HIST-012（FR-106）JSON round-trip 保持值与类型
# ---------------------------------------------------------------------------
def test_hist_012_inputs_round_trip_preserves_json_types(tmp_path):
    inputs = {
        "question": "年度营收是多少？",
        "nested": {"列表": [1, 2.5, None, True, False, "文本"], "深": {"k": [{"a": 1}]}},
        "none": None,
        "flag": True,
        "count": 42,
        "ratio": 0.125,
        "tiny": 1e-7,
        "empty_list": [],
        "empty_dict": {},
    }
    store = tmp_path / "runs.db"
    run = _make_store_client(tmp_path, store).post(
        "/v1/workflows/run", json=_run_body(inputs=inputs), headers=AUTH
    )
    assert run.status_code == 200
    run_id = run.json()["workflow_run_id"]

    resp = _make_store_client(tmp_path, store).get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    got = resp.json()["inputs"]
    assert got == inputs
    assert type(got["count"]) is int
    assert type(got["ratio"]) is float
    assert type(got["flag"]) is bool
    assert type(got["nested"]) is dict
    assert type(got["nested"]["列表"]) is list
    assert got["nested"]["列表"][2] is None
    assert resp.json()["outputs"] == run.json()["data"]["outputs"]


# ---------------------------------------------------------------------------
# HIST-013（FR-105）不可用 / 损坏 / 不兼容存储 -> 脱敏 503，不回退、不覆盖
# ---------------------------------------------------------------------------
def test_hist_013_directory_as_database_is_503(tmp_path):
    store = tmp_path / "runs-as-dir.db"
    store.mkdir()
    client = _make_store_client(tmp_path, store)
    resp = client.post("/v1/workflows/run", json=_secret_body(), headers=AUTH)
    _assert_history_unavailable(resp, str(store), store.name, "app-key-1", BODY_SECRET)
    get = client.get("/v1/workflows/run/any-id", headers=AUTH)
    _assert_history_unavailable(get, str(store), store.name, "app-key-1")
    assert store.is_dir() and list(store.iterdir()) == []


def test_hist_013_missing_parent_directory_is_503_and_not_created(tmp_path):
    store = tmp_path / "缺失 目录" / "runs.db"
    client = _make_store_client(tmp_path, store)
    resp = client.post("/v1/workflows/run", json=_secret_body(), headers=AUTH)
    _assert_history_unavailable(resp, str(store), "缺失 目录", "app-key-1", BODY_SECRET)
    assert not store.parent.exists()


def test_hist_013_corrupt_database_file_is_503_and_untouched(tmp_path):
    store = tmp_path / "runs.db"
    garbage = b"this is not a sqlite database \x00\xff" * 200
    store.write_bytes(garbage)
    client = _make_store_client(tmp_path, store)
    post = client.post("/v1/workflows/run", json=_secret_body(), headers=AUTH)
    _assert_history_unavailable(post, str(store), "app-key-1", BODY_SECRET)
    get = client.get("/v1/workflows/run/any-id", headers=AUTH)
    _assert_history_unavailable(get, str(store), "app-key-1")
    assert store.read_bytes() == garbage


@pytest.mark.parametrize("variant", ["future_version", "foreign_table"])
def test_hist_013_incompatible_schema_is_503_and_untouched(tmp_path, variant):
    store = tmp_path / "runs.db"
    conn = sqlite3.connect(store)
    try:
        if variant == "future_version":
            conn.execute("CREATE TABLE dify_public_runs (something_else TEXT)")
            conn.execute("PRAGMA user_version = 999")
        else:
            conn.execute("CREATE TABLE unrelated_business_data (secret TEXT)")
            conn.execute("INSERT INTO unrelated_business_data VALUES ('keep me')")
        conn.commit()
    finally:
        conn.close()
    original = store.read_bytes()

    client = _make_store_client(tmp_path, store)
    post = client.post("/v1/workflows/run", json=_secret_body(), headers=AUTH)
    _assert_history_unavailable(post, str(store), "app-key-1", "keep me", BODY_SECRET)
    get = client.get("/v1/workflows/run/any-id", headers=AUTH)
    _assert_history_unavailable(get, str(store), "app-key-1", "keep me")
    assert store.read_bytes() == original


def test_hist_013_errors_are_not_logged_with_path_or_key(tmp_path, caplog):
    store = tmp_path / "runs-as-dir.db"
    store.mkdir()
    client = _make_store_client(tmp_path, store)
    resp = client.post("/v1/workflows/run", json=_secret_body(), headers=AUTH)
    _assert_history_unavailable(resp, BODY_SECRET)
    assert str(store) not in caplog.text
    assert BODY_SECRET not in caplog.text
    assert "app-key-1" not in caplog.text


# ---------------------------------------------------------------------------
# HIST-014（FR-106）已归属记录损坏 -> owner 看到 503；其它 key 先按归属筛选仍 404
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[1, 2, 3]",
        json.dumps({"id": "x"}),  # 缺必需字段
        json.dumps(
            {"id": "x", "workflow_id": "w", "status": "succeeded", "total_steps": "many"}
        ),  # 非法字段类型
    ],
    ids=["broken_json", "not_object", "missing_fields", "bad_field_type"],
)
def test_hist_014_corrupt_record_is_503_for_owner_404_for_others(tmp_path, payload):
    store = tmp_path / "runs.db"
    apps = f"app-key-1={SEQ_YML};app-key-2={SEQ_YML}"
    client = _make_store_client(tmp_path, store, apps=apps)
    run_id = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    fresh = _make_store_client(tmp_path, store, apps=apps)
    assert fresh.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200

    # 存储契约：表 dify_public_runs，列 run_id / payload（JSON 文本）。
    conn = sqlite3.connect(store)
    try:
        updated = conn.execute(
            "UPDATE dify_public_runs SET payload = ? WHERE run_id = ?", (payload, run_id)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert updated == 1

    reader = _make_store_client(tmp_path, store, apps=apps)
    owner = reader.get(f"/v1/workflows/run/{run_id}", headers=AUTH)
    _assert_history_unavailable(owner, str(store), "app-key-1", "not json")
    other = reader.get(f"/v1/workflows/run/{run_id}", headers=AUTH_2)
    assert other.status_code == 404
    assert other.json()["code"] == "not_found"


def test_hist_014_record_whose_id_mismatches_is_503(tmp_path):
    """记录正文的 id 与查询 run ID 不符属于结构损坏，不能当成另一条记录返回。"""
    store = tmp_path / "runs.db"
    client = _make_store_client(tmp_path, store)
    first = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    second = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    fresh = _make_store_client(tmp_path, store)
    assert fresh.get(f"/v1/workflows/run/{first}", headers=AUTH).status_code == 200

    conn = sqlite3.connect(store)
    try:
        (second_payload,) = conn.execute(
            "SELECT payload FROM dify_public_runs WHERE run_id = ?", (second,)
        ).fetchone()
        conn.execute(
            "UPDATE dify_public_runs SET payload = ? WHERE run_id = ?", (second_payload, first)
        )
        conn.commit()
    finally:
        conn.close()

    resp = _make_store_client(tmp_path, store).get(f"/v1/workflows/run/{first}", headers=AUTH)
    _assert_history_unavailable(resp)


# ---------------------------------------------------------------------------
# HIST-015（FR-105）写入 / 提交失败：503、不重试、streaming 不先发成功终态、无半条记录
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["blocking", "streaming"])
def test_hist_015_commit_failure_is_503_without_retry_or_partial_record(
    tmp_path, monkeypatch, mode
):
    store = tmp_path / "runs.db"
    client = _make_store_client(tmp_path, store)
    kept = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert (
        _make_store_client(tmp_path, store)
        .get(f"/v1/workflows/run/{kept}", headers=AUTH)
        .status_code
        == 200
    )

    calls = _count_executions(monkeypatch)
    with monkeypatch.context() as patch:
        _inject_commit_failure(patch)
        resp = client.post("/v1/workflows/run", json=_secret_body(response_mode=mode), headers=AUTH)
    _assert_history_unavailable(resp, str(store), "app-key-1", BODY_SECRET)
    assert "workflow_finished" not in resp.text
    assert "succeeded" not in resp.text
    assert calls == [1]  # 执行一次，不自动重试

    # 既有记录仍可读；失败 run 没有留下半条记录。
    reader = _make_store_client(tmp_path, store)
    assert reader.get(f"/v1/workflows/run/{kept}", headers=AUTH).status_code == 200
    conn = sqlite3.connect(store)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM dify_public_runs").fetchone()
    finally:
        conn.close()
    assert count == 1


def test_hist_015_readonly_database_write_is_503_and_keeps_existing(tmp_path):
    """不依赖 monkeypatch 的写入失败：库文件只读（跨平台 os.chmod）。"""
    import stat

    store = tmp_path / "runs.db"
    client = _make_store_client(tmp_path, store)
    kept = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert (
        _make_store_client(tmp_path, store)
        .get(f"/v1/workflows/run/{kept}", headers=AUTH)
        .status_code
        == 200
    )
    os.chmod(store, stat.S_IREAD)
    try:
        resp = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
        _assert_history_unavailable(resp, str(store), "app-key-1")
    finally:
        os.chmod(store, stat.S_IREAD | stat.S_IWRITE)
    assert (
        _make_store_client(tmp_path, store)
        .get(f"/v1/workflows/run/{kept}", headers=AUTH)
        .status_code
        == 200
    )


# ---------------------------------------------------------------------------
# HIST-016（FR-108）多个 app 共享同一文件；有限并发写入无 thread-affinity 异常
# ---------------------------------------------------------------------------
def test_hist_016_two_live_apps_share_committed_records(tmp_path):
    store = tmp_path / "runs.db"
    writer = _make_store_client(tmp_path, store)
    reader = _make_store_client(tmp_path, store)  # 两个 app 同时存活
    run_id = writer.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert reader.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
    back = reader.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
        "workflow_run_id"
    ]
    assert writer.get(f"/v1/workflows/run/{back}", headers=AUTH).status_code == 200


def test_hist_016_concurrent_writes_from_threads(tmp_path, monkeypatch):
    import ragspine.service.api.dify_public as dify_public

    monkeypatch.setattr(dify_public, "_MAX_RUNS_PER_OWNER", 5, raising=False)
    store = tmp_path / "runs.db"
    clients = [_make_store_client(tmp_path, store) for _ in range(4)]

    def _post(i: int) -> tuple[int, str]:
        resp = clients[i % len(clients)].post("/v1/workflows/run", json=_run_body(), headers=AUTH)
        return resp.status_code, resp.json().get("workflow_run_id", "")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_post, range(8)))
    assert [status for status, _ in results] == [200] * 8

    reader = _make_store_client(tmp_path, store)
    found = [
        run_id
        for _, run_id in results
        if reader.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
    ]
    assert len(found) == 5  # 容量有界：同一 owner 只留最近 5 条


# ---------------------------------------------------------------------------
# HIST-017（FR-004 / FR-109）历史不构成执行入口
# ---------------------------------------------------------------------------
def test_hist_017_history_readable_when_execution_disabled_but_no_new_runs(tmp_path):
    store = tmp_path / "runs.db"
    run_id = (
        _make_store_client(tmp_path, store)
        .post("/v1/workflows/run", json=_run_body(), headers=AUTH)
        .json()["workflow_run_id"]
    )

    disabled = _make_store_client(tmp_path, store, run_enabled=False)
    assert disabled.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
    assert disabled.get(f"/v1/workflows/run/{run_id}").status_code == 401
    post = disabled.post("/v1/workflows/run", json=_run_body(), headers=AUTH)
    assert post.status_code == 400
    assert post.json()["code"] == "app_unavailable"
    # 仅配置存储路径不会打开执行开关。
    assert _store_config(tmp_path, store, run_enabled=False).dify_run_enabled is False


# ---------------------------------------------------------------------------
# HIST-018（FR-108）关闭 app 后无遗留句柄：可重命名 / 删除数据库文件
# ---------------------------------------------------------------------------
def _open_connections() -> set:
    gc.collect()
    opened = set()
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.total_changes  # noqa: B018 —— 已关闭连接会抛 ProgrammingError
            except sqlite3.ProgrammingError:
                continue
            opened.add(id(obj))
    return opened


def test_hist_018_no_leaked_handles_after_app_closes(tmp_path):
    store = tmp_path / "runs.db"
    with _make_store_client(tmp_path, store) as client:
        baseline = _open_connections()
        run_id = client.post("/v1/workflows/run", json=_run_body(), headers=AUTH).json()[
            "workflow_run_id"
        ]
        client.post("/v1/workflows/run", json=_run_body(response_mode="streaming"), headers=AUTH)
        assert client.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 200
        # 请求结束后不持有额外 sqlite 连接（短生命周期连接）。
        assert _open_connections() - baseline == set()

    assert store.is_file()
    moved = tmp_path / "runs-moved.db"
    store.rename(moved)
    moved.unlink()
    for leftover in _history_files(store):
        leftover.unlink()
    assert not store.exists() and not moved.exists()

    # 原路径重新可用，且不依赖旧进程对象：新库里没有旧记录。
    fresh = _make_store_client(tmp_path, store)
    assert fresh.get(f"/v1/workflows/run/{run_id}", headers=AUTH).status_code == 404
    assert fresh.post("/v1/workflows/run", json=_run_body(), headers=AUTH).status_code == 200
