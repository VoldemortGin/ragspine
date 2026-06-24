"""服务层 /v1/dify/* HTTP 行为测试（薄适配层，只验证外部行为）。

覆盖 analyze（只建议，安全）/ compile（代码字符串，安全）/ compile_unsupported（带
NotImplementedError 钩子的 warning）/ 编译错误整形 400。注入 MockProvider + FakeQueue，
零真实 LLM API（TestClient）。run 端点的安全三层与异步测试见后续阶段。
"""

import os
from pathlib import Path

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.tasks.task_queue import FakeQueue

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_client(tmp_path, *, run_enabled=False, timeout_s=10.0, provider=None,
                 isolation="inprocess"):
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=run_enabled,
        dify_run_timeout_s=timeout_s,
        dify_run_isolation=isolation,
    )
    app = create_app(
        config, provider=provider or MockProvider(), queue=FakeQueue()
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return _make_client(tmp_path)


# ---------------------------------------------------------------------------
# ANALYZE — 只跑静态优化分析，零代码生成、零 API
# ---------------------------------------------------------------------------
def test_dify_analyze_returns_suggestions(client):
    resp = client.post("/v1/dify/analyze", json={"yaml": _fixture("parallel.yml")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    # parallel.yml 触发一条并行机会建议
    assert len(body["suggestions"]) >= 1
    s = body["suggestions"][0]
    assert s["rule_id"]
    assert s["severity"] in ("high", "medium", "low", "info")
    assert s["category"]
    assert s["title"]


def test_dify_analyze_seq_has_no_suggestions(client):
    resp = client.post("/v1/dify/analyze", json={"yaml": _fixture("seq.yml")})
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


def test_dify_analyze_bad_app_mode_is_400(client):
    bad = "app:\n  mode: chat\n  name: x\nkind: app\nversion: \"0.1.5\"\n" \
          "workflow:\n  graph:\n    nodes: []\n    edges: []\n"
    resp = client.post("/v1/dify/analyze", json={"yaml": bad})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "dify.unsupported_app_mode"
    assert err["request_id"]


# ---------------------------------------------------------------------------
# COMPILE — 编译成纯 Python 代码字符串（不执行）
# ---------------------------------------------------------------------------
def test_dify_compile_returns_code(client):
    resp = client.post("/v1/dify/compile", json={"yaml": _fixture("seq.yml")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    assert body["entrypoint"] == "run_workflow"
    assert "def run_workflow(" in body["code"]
    # 客户端不可注入 provider_expr：生成代码默认 MockProvider()（离线）
    assert "MockProvider()" in body["code"]
    assert body["warnings"] == []


def test_dify_compile_unsupported_node_emits_warning(client):
    # agent_tool.yml 含 tool 节点 -> 生成带 NotImplementedError 的 @function_tool 占位 + warning
    resp = client.post("/v1/dify/compile", json={"yaml": _fixture("agent_tool.yml")})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["warnings"]) >= 1
    assert "NotImplementedError" in body["warnings"][0]
    # 仍生成可读骨架代码（compile 不拒，run 才在 L0 静态闸拒）
    assert "def run_workflow(" in body["code"]


def test_dify_compile_malformed_yaml_is_400(client):
    resp = client.post("/v1/dify/compile", json={"yaml": ": : not valid : ["})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "dify.compile"


def test_dify_compile_bad_target_is_400(client):
    resp = client.post(
        "/v1/dify/compile",
        json={"yaml": _fixture("seq.yml"), "target": "nonsense"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "dify.unsupported_target"


# ---------------------------------------------------------------------------
# RUN — 编译 + 受限执行（信任边界，默认关）
# ---------------------------------------------------------------------------
def test_dify_run_disabled_by_default(tmp_path):
    client = _make_client(tmp_path, run_enabled=False)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "dify.run_disabled"


def test_dify_run_mock_when_enabled(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    assert "result" in body["result"]
    assert isinstance(body["result"]["result"], str)
    assert body["warnings"] == []


def test_dify_run_static_gate_rejects_unsupported_node_without_exec(tmp_path):
    # agent_tool.yml 含 tool 占位 -> L0 闸拒（warnings 非空）-> 422，且绝不 exec。
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("agent_tool.yml"), "inputs": {}}
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["type"] == "dify.unsafe"
    # 断言确实没执行：骨架钩子若被 exec 会抛 NotImplementedError（dify.run_error/500），
    # 这里拿到的是 422 的 dify.unsafe，证明拒在 exec 之前。
    assert "NotImplementedError" in err["message"] or "骨架" in err["message"]


def test_dify_run_import_allowlist_via_unsupported_http_node(tmp_path):
    # http-request 节点同样生成 NotImplementedError 骨架 + warning -> L0 闸 422（未执行）。
    # 这条间接验证「越权能力的节点不会被跑」——与单元层的 import 白名单互补。
    http_yaml = (
        "app:\n  mode: workflow\n  name: x\nkind: app\nversion: \"0.1.5\"\n"
        "workflow:\n  graph:\n    nodes:\n"
        "      - id: start_1\n        data: {type: start, title: s, variables: []}\n"
        "      - id: http_1\n        data: {type: http-request, title: h}\n"
        "      - id: end_1\n        data: {type: end, title: e, outputs: []}\n"
        "    edges:\n"
        "      - {source: start_1, target: http_1, sourceHandle: source}\n"
        "      - {source: http_1, target: end_1, sourceHandle: source}\n"
    )
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": http_yaml, "inputs": {}})
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "dify.unsafe"


def test_dify_run_timeout(tmp_path):
    # 用极小超时 + 一个会循环的 iteration（MockProvider 极快，故构造长循环不易）；
    # 改用 compile 出 seq 后无法人为拖时——直接用极小 timeout 跑含 LLM 节点的 qa_fold，
    # 实际更稳的做法：用 0 附近超时让线程 join 立即超时（生成代码首次 import 即 >timeout）。
    client = _make_client(tmp_path, run_enabled=True, timeout_s=0.0001)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("qa_fold.yml"), "inputs": {"question": "q"}}
    )
    # 极小超时下，exec/调用几乎必然超时 -> 400 dify.timeout
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "dify.timeout"


def test_dify_run_compile_error_is_400(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": ": : bad : [", "inputs": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "dify.compile"


def test_dify_run_provider_from_config_not_client(tmp_path):
    # provider 由服务端注入：客户端 body 里【没有】provider 字段也能跑（且无法注入 provider_expr）。
    # 用一个记录调用次数的 spy provider 证明服务端 provider 被用上。
    class SpyProvider:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, tools=None):
            self.calls += 1
            return MockProvider().chat(messages, tools=tools)

    spy = SpyProvider()
    client = _make_client(tmp_path, run_enabled=True, provider=spy)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}}
    )
    assert resp.status_code == 200
    # seq.yml 有一个 llm 节点 -> 服务端 provider 被调用一次
    assert spy.calls == 1
    # 请求体里不接受 provider_expr：即便塞了也被 pydantic 忽略（schema 无此字段）
    resp2 = client.post(
        "/v1/dify/run",
        json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"},
              "provider_expr": "__import__('os')"},
    )
    assert resp2.status_code == 200  # 多余字段被忽略，未注入


# ---------------------------------------------------------------------------
# RUN ASYNC — 入队执行，状态经 GET /v1/jobs/{id}（复用既有 job 端点）
# ---------------------------------------------------------------------------
def test_dify_run_async_enqueue_and_poll(tmp_path):
    # FakeQueue 内联执行：enqueue 即跑完，立刻可取 finished 结果。
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run/jobs",
        json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert job_id

    status = client.get(f"/v1/jobs/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["id"] == job_id
    assert body["status"] == "finished"
    assert "result" in body["result"]
    assert "result" in body["result"]["result"]  # job 包了一层 {"result": {...}}


def test_dify_run_async_disabled_by_default(tmp_path):
    client = _make_client(tmp_path, run_enabled=False)
    resp = client.post(
        "/v1/dify/run/jobs",
        json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "dify.run_disabled"


def test_dify_run_async_static_gate_rejects_before_enqueue(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run/jobs", json={"yaml": _fixture("agent_tool.yml"), "inputs": {}}
    )
    # 入队前 L0 闸拒 -> 422（绝不入队、绝不执行）
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "dify.unsafe"


def test_dify_run_async_compile_error_before_enqueue(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run/jobs", json={"yaml": ": : bad : [", "inputs": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "dify.compile"


# ---------------------------------------------------------------------------
# ISOLATION — subprocess 隔离端到端（Linux 真子进程；macOS/Windows 回落 L1）
# ---------------------------------------------------------------------------
def test_dify_run_subprocess_isolation_end_to_end(tmp_path):
    client = _make_client(tmp_path, run_enabled=True, isolation="subprocess")
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}}
    )
    assert resp.status_code == 200
    # 无论真子进程还是回落 L1，结果一致：provider 仍由服务端 config 决定
    assert "result" in resp.json()["result"]
