"""服务层 n8n 公共 REST API（/api/v1/* + /webhook/*）HTTP 行为测试。

对外形状对齐 n8n 官方 Public API：X-N8N-API-KEY 鉴权（错误体恒 {"message": ...}）、
workflows CRUD/activate/deactivate/offset 型 cursor 分页、executions 列表/详情/删除
（lastId 型 cursor、includeData）、无鉴权 /webhook/{path} 触发（active workflow 的
webhook 节点匹配后经 n8n→dify 完整复用 dify run 管线）。注入 MockProvider +
FakeQueue，零真实 LLM API（TestClient）。
"""

import base64
import json
import os
import time

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.n8n_public.router import _merge_inputs
from ragspine.service.n8n_public.store import N8nStore
from ragspine.service.tasks.task_queue import FakeQueue

N8N_FIXTURES = ROOT_DIR / "tests" / "n8n" / "fixtures"

API_KEY = "test-key"
HEADERS = {"X-N8N-API-KEY": API_KEY}


def _webhook_body() -> dict:
    """webhook.json fixture -> POST /api/v1/workflows 的合法 body（四必填字段）。"""
    src = json.loads((N8N_FIXTURES / "webhook.json").read_text(encoding="utf-8"))
    return {k: src[k] for k in ("name", "nodes", "connections", "settings")}


def _minimal_body(name: str = "Minimal") -> dict:
    return {"name": name, "nodes": [], "connections": {}, "settings": {}}


def _make_client(tmp_path, *, run_enabled=False, api_key=API_KEY, provider=None):
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=run_enabled,
        n8n_api_key=api_key,
        n8n_store_path=str(tmp_path / "n8n_store"),
    )
    app = create_app(
        config, provider=provider or MockProvider(), queue=FakeQueue()
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return _make_client(tmp_path)


def _create(client, body) -> dict:
    resp = client.post("/api/v1/workflows", headers=HEADERS, json=body)
    assert resp.status_code == 200
    return resp.json()


def _activate(client, workflow_id: str) -> None:
    resp = client.post(f"/api/v1/workflows/{workflow_id}/activate", headers=HEADERS)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 鉴权矩阵（/api/v1/* 生效；错误体恒 {"message": ...}）
# ---------------------------------------------------------------------------
def test_api_v1_disabled_without_server_key(tmp_path):
    client = _make_client(tmp_path, api_key=None)
    resp = client.get("/api/v1/workflows", headers=HEADERS)
    assert resp.status_code == 401
    body = resp.json()
    assert set(body) == {"message"}
    assert "RAGSPINE_N8N_API_KEY" in body["message"]


def test_missing_api_key_header_is_401(client):
    resp = client.get("/api/v1/workflows")
    assert resp.status_code == 401
    assert resp.json() == {"message": "'X-N8N-API-KEY' header required"}


def test_wrong_api_key_is_401(client):
    resp = client.get("/api/v1/workflows", headers={"X-N8N-API-KEY": "nope"})
    assert resp.status_code == 401
    assert resp.json() == {"message": "unauthorized"}


def test_valid_api_key_passes(client):
    resp = client.get("/api/v1/workflows", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"data": [], "nextCursor": None}


# ---------------------------------------------------------------------------
# workflows CRUD 全流程
# ---------------------------------------------------------------------------
def test_workflow_crud_roundtrip(client):
    wf = _create(client, _minimal_body("Demo"))
    assert wf["id"]
    assert wf["name"] == "Demo"
    assert wf["active"] is False
    assert wf["tags"] == []
    assert wf["staticData"] is None
    assert wf["createdAt"].endswith("Z")
    assert wf["updatedAt"] == wf["createdAt"]
    assert wf["nodes"] == [] and wf["connections"] == {} and wf["settings"] == {}

    got = client.get(f"/api/v1/workflows/{wf['id']}", headers=HEADERS)
    assert got.status_code == 200
    assert got.json() == wf

    listed = client.get("/api/v1/workflows", headers=HEADERS)
    assert listed.status_code == 200
    body = listed.json()
    assert [w["id"] for w in body["data"]] == [wf["id"]]
    assert body["nextCursor"] is None

    time.sleep(0.005)  # 保证毫秒级 updatedAt 前进
    updated = client.put(
        f"/api/v1/workflows/{wf['id']}", headers=HEADERS, json=_minimal_body("Renamed")
    )
    assert updated.status_code == 200
    after = updated.json()
    assert after["name"] == "Renamed"
    assert after["id"] == wf["id"]
    assert after["createdAt"] == wf["createdAt"]
    assert after["updatedAt"] > wf["updatedAt"]  # ISO-8601 字符串可字典序比较

    deleted = client.delete(f"/api/v1/workflows/{wf['id']}", headers=HEADERS)
    assert deleted.status_code == 200
    assert deleted.json()["id"] == wf["id"]

    missing = client.get(f"/api/v1/workflows/{wf['id']}", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json() == {"message": "Not Found"}


def test_put_missing_workflow_is_404(client):
    resp = client.put(
        "/api/v1/workflows/nonexistent", headers=HEADERS, json=_minimal_body()
    )
    assert resp.status_code == 404
    assert resp.json() == {"message": "Not Found"}


# ---------------------------------------------------------------------------
# POST/PUT body 严格校验（express-openapi-validator 的 message 文本）
# ---------------------------------------------------------------------------
def test_create_with_readonly_field_is_400(client):
    resp = client.post(
        "/api/v1/workflows", headers=HEADERS, json=_minimal_body() | {"active": True}
    )
    assert resp.status_code == 400
    assert resp.json() == {"message": "request/body/active is read-only"}


def test_create_with_unknown_field_is_400(client):
    resp = client.post(
        "/api/v1/workflows", headers=HEADERS, json=_minimal_body() | {"bogus": 1}
    )
    assert resp.status_code == 400
    assert resp.json() == {"message": "request/body must NOT have additional properties"}


def test_create_missing_required_is_400(client):
    body = _minimal_body()
    del body["name"]
    resp = client.post("/api/v1/workflows", headers=HEADERS, json=body)
    assert resp.status_code == 400
    assert resp.json() == {"message": "request/body must have required property 'name'"}


def test_put_validates_like_post(client):
    wf = _create(client, _minimal_body())
    resp = client.put(
        f"/api/v1/workflows/{wf['id']}", headers=HEADERS,
        json=_minimal_body() | {"id": "injected"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"message": "request/body/id is read-only"}


# ---------------------------------------------------------------------------
# activate / deactivate
# ---------------------------------------------------------------------------
def test_activate_and_deactivate(client):
    wf = _create(client, _minimal_body())
    on = client.post(f"/api/v1/workflows/{wf['id']}/activate", headers=HEADERS)
    assert on.status_code == 200
    assert on.json()["active"] is True
    assert on.json()["id"] == wf["id"]
    off = client.post(f"/api/v1/workflows/{wf['id']}/deactivate", headers=HEADERS)
    assert off.status_code == 200
    assert off.json()["active"] is False


def test_activate_missing_workflow_is_404(client):
    resp = client.post("/api/v1/workflows/nonexistent/activate", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json() == {"message": "Not Found"}


# ---------------------------------------------------------------------------
# workflows 列表过滤 + offset 型 cursor 分页
# ---------------------------------------------------------------------------
def test_workflow_list_filters(client):
    alpha = _create(client, _minimal_body("alpha"))
    _create(client, _minimal_body("beta"))
    _activate(client, alpha["id"])

    by_name = client.get(
        "/api/v1/workflows", headers=HEADERS, params={"name": "alpha"}
    ).json()
    assert [w["name"] for w in by_name["data"]] == ["alpha"]

    by_active = client.get(
        "/api/v1/workflows", headers=HEADERS, params={"active": "true"}
    ).json()
    assert [w["id"] for w in by_active["data"]] == [alpha["id"]]


def test_workflow_pagination_with_cursor(client):
    for i in range(3):
        _create(client, _minimal_body(f"wf-{i}"))

    page1 = client.get(
        "/api/v1/workflows", headers=HEADERS, params={"limit": 2}
    ).json()
    assert len(page1["data"]) == 2
    assert page1["nextCursor"]
    assert json.loads(base64.b64decode(page1["nextCursor"])) == {"limit": 2, "offset": 2}

    page2 = client.get(
        "/api/v1/workflows", headers=HEADERS, params={"cursor": page1["nextCursor"]}
    ).json()
    assert len(page2["data"]) == 1
    assert page2["nextCursor"] is None
    # 两页拼接无重复 = 稳定序全量覆盖
    all_ids = [w["id"] for w in page1["data"] + page2["data"]]
    assert len(set(all_ids)) == 3


def test_invalid_cursor_is_400(client):
    resp = client.get(
        "/api/v1/workflows", headers=HEADERS, params={"cursor": "@@not-base64@@"}
    )
    assert resp.status_code == 400
    assert set(resp.json()) == {"message"}


# ---------------------------------------------------------------------------
# webhook 触发（无鉴权；经 n8n→dify 完整复用 dify run 管线）
# ---------------------------------------------------------------------------
def _setup_webhook_workflow(client) -> dict:
    wf = _create(client, _webhook_body())
    _activate(client, wf["id"])
    return wf


def test_webhook_trigger_success_and_execution_recorded(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    wf = _setup_webhook_workflow(client)

    resp = client.post("/webhook/demo-hook", json={"question": "hi"})  # 无鉴权 header
    assert resp.status_code == 200
    result = resp.json()
    assert isinstance(result["format_output"], str)

    listed = client.get(
        "/api/v1/executions", headers=HEADERS, params={"workflowId": wf["id"]}
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["nextCursor"] is None
    (execution,) = body["data"]
    assert isinstance(execution["id"], int)
    assert execution["finished"] is True
    assert execution["mode"] == "webhook"
    assert execution["status"] == "success"
    assert execution["workflowId"] == wf["id"]
    assert execution["startedAt"] and execution["stoppedAt"]
    assert execution["retryOf"] is None
    assert execution["retrySuccessId"] is None
    assert execution["waitTill"] is None
    assert execution["customData"] == {}
    assert "data" not in execution  # 不带 includeData 时无 data

    detail = client.get(
        f"/api/v1/executions/{execution['id']}", headers=HEADERS,
        params={"includeData": "true"},
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["format_output"] == result["format_output"]

    bare = client.get(f"/api/v1/executions/{execution['id']}", headers=HEADERS)
    assert bare.status_code == 200
    assert "data" not in bare.json()


def test_webhook_not_registered_when_inactive(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    _create(client, _webhook_body())  # 不 activate -> 不注册
    resp = client.post("/webhook/demo-hook", json={"question": "hi"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 404
    assert body["message"] == 'The requested webhook "POST demo-hook" is not registered.'
    assert "hint" in body


def test_webhook_method_mismatch_is_404(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    _setup_webhook_workflow(client)  # httpMethod=POST
    resp = client.get("/webhook/demo-hook")
    assert resp.status_code == 404
    assert 'GET demo-hook' in resp.json()["message"]


def test_webhook_disabled_run_is_503(tmp_path):
    client = _make_client(tmp_path, run_enabled=False)
    _setup_webhook_workflow(client)
    resp = client.post("/webhook/demo-hook", json={"question": "hi"})
    assert resp.status_code == 503
    assert "RAGSPINE_DIFY_RUN_ENABLED" in resp.json()["message"]


def test_webhook_default_method_is_get_and_path_stripped(tmp_path):
    # 节点缺 httpMethod -> 默认 GET；parameters.path 两侧 strip "/" 后匹配。
    client = _make_client(tmp_path, run_enabled=True)
    body = {
        "name": "Get Hook",
        "nodes": [
            {"id": "g1", "name": "Hook", "type": "n8n-nodes-base.webhook",
             "typeVersion": 2, "position": [0, 0],
             "parameters": {"path": "/get-hook/"}},
            {"id": "g2", "name": "Echo", "type": "n8n-nodes-base.set",
             "typeVersion": 3.4, "position": [220, 0],
             "parameters": {"assignments": {"assignments": [
                 {"id": "a1", "name": "result", "value": "ok", "type": "string"}]},
                 "options": {}}},
        ],
        "connections": {"Hook": {"main": [[{"node": "Echo", "type": "main", "index": 0}]]}},
        "settings": {},
    }
    wf = _create(client, body)
    _activate(client, wf["id"])
    resp = client.get("/webhook/get-hook", params={"question": "hi"})
    assert resp.status_code == 200
    assert resp.json() == {"echo": "ok"}


def test_webhook_run_failure_is_500_and_records_error(tmp_path):
    # httpRequest -> n8n-passthrough 骨架 -> L0 静态闸拒 -> 500 + error execution。
    client = _make_client(tmp_path, run_enabled=True)
    body = {
        "name": "Err Hook",
        "nodes": [
            {"id": "e1", "name": "Hook", "type": "n8n-nodes-base.webhook",
             "typeVersion": 2, "position": [0, 0],
             "parameters": {"httpMethod": "POST", "path": "err-hook"}},
            {"id": "e2", "name": "Fetch", "type": "n8n-nodes-base.httpRequest",
             "typeVersion": 4.2, "position": [220, 0],
             "parameters": {"url": "https://example.com"}},
        ],
        "connections": {"Hook": {"main": [[{"node": "Fetch", "type": "main", "index": 0}]]}},
        "settings": {},
    }
    wf = _create(client, body)
    _activate(client, wf["id"])

    resp = client.post("/webhook/err-hook", json={})
    assert resp.status_code == 500
    assert resp.json() == {"message": "Error in workflow"}

    listed = client.get(
        "/api/v1/executions", headers=HEADERS,
        params={"workflowId": wf["id"], "includeData": "true"},
    ).json()
    (execution,) = listed["data"]
    assert execution["status"] == "error"
    assert execution["finished"] is False
    assert "error" in execution["data"]


# ---------------------------------------------------------------------------
# webhook inputs 映射：query 与 JSON body 合并，冲突时 body 优先
# ---------------------------------------------------------------------------
def test_merge_inputs_body_wins_over_query():
    merged = _merge_inputs({"a": "1", "question": "from-query"}, {"question": "from-body"})
    assert merged == {"a": "1", "question": "from-body"}


def test_merge_inputs_non_dict_body_uses_query_only():
    assert _merge_inputs({"a": "1"}, ["not", "a", "dict"]) == {"a": "1"}
    assert _merge_inputs({"a": "1"}, None) == {"a": "1"}
    assert _merge_inputs({}, {"b": 2}) == {"b": 2}


# ---------------------------------------------------------------------------
# executions：降序、过滤、404、删除、lastId 型 cursor 分页
# ---------------------------------------------------------------------------
def test_executions_list_desc_filter_and_delete(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    wf = _setup_webhook_workflow(client)
    for question in ("one", "two"):
        assert client.post(
            "/webhook/demo-hook", json={"question": question}
        ).status_code == 200

    listed = client.get("/api/v1/executions", headers=HEADERS).json()
    ids = [e["id"] for e in listed["data"]]
    assert len(ids) == 2
    assert ids == sorted(ids, reverse=True)  # 最新在前

    by_wf = client.get(
        "/api/v1/executions", headers=HEADERS, params={"workflowId": wf["id"]}
    ).json()
    assert len(by_wf["data"]) == 2
    none_wf = client.get(
        "/api/v1/executions", headers=HEADERS, params={"workflowId": "nope"}
    ).json()
    assert none_wf["data"] == []

    by_status = client.get(
        "/api/v1/executions", headers=HEADERS, params={"status": "success"}
    ).json()
    assert len(by_status["data"]) == 2
    err_status = client.get(
        "/api/v1/executions", headers=HEADERS, params={"status": "error"}
    ).json()
    assert err_status["data"] == []

    missing = client.get("/api/v1/executions/99999", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json() == {"message": "Not Found"}

    deleted = client.delete(f"/api/v1/executions/{ids[0]}", headers=HEADERS)
    assert deleted.status_code == 200
    assert deleted.json()["id"] == ids[0]
    remaining = client.get("/api/v1/executions", headers=HEADERS).json()
    assert [e["id"] for e in remaining["data"]] == [ids[1]]


def test_executions_pagination_last_id_cursor(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    _setup_webhook_workflow(client)
    for question in ("a", "b", "c"):
        assert client.post(
            "/webhook/demo-hook", json={"question": question}
        ).status_code == 200

    page1 = client.get(
        "/api/v1/executions", headers=HEADERS, params={"limit": 2}
    ).json()
    assert [e["id"] for e in page1["data"]] == [3, 2]
    assert json.loads(base64.b64decode(page1["nextCursor"])) == {"lastId": 2, "limit": 2}

    page2 = client.get(
        "/api/v1/executions", headers=HEADERS, params={"cursor": page1["nextCursor"]}
    ).json()
    assert [e["id"] for e in page2["data"]] == [1]
    assert page2["nextCursor"] is None


# ---------------------------------------------------------------------------
# 存储层（文件存储；坏文件跳过；execution cap）
# ---------------------------------------------------------------------------
def test_execution_store_caps_at_200(tmp_path):
    store = N8nStore(tmp_path / "n8n_store")
    for _ in range(205):
        store.create_execution({"workflowId": "w", "status": "success"})
    records = store.list_executions()
    assert len(records) == 200
    assert records[0]["id"] == 205
    assert records[-1]["id"] == 6  # 最旧（id 最小）的被删


def test_store_skips_corrupt_files(tmp_path):
    root = tmp_path / "n8n_store"
    store = N8nStore(root)
    store.save_workflow(
        {"id": "good", "name": "ok", "createdAt": "2026-01-01T00:00:00.000Z"}
    )
    (root / "workflows" / "bad.json").write_text("{not json", encoding="utf-8")
    assert [w["id"] for w in store.list_workflows()] == ["good"]
