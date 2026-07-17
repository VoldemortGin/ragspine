"""服务层 /v1/dify/* HTTP 行为测试（薄适配层，只验证外部行为）。

覆盖 analyze（只建议，安全）/ compile（代码字符串，安全）/ compile_unsupported（带
NotImplementedError 钩子的 warning）/ 编译错误整形 400。注入 MockProvider + FakeQueue，
零真实 LLM API（TestClient）。run 端点的安全三层与异步测试见后续阶段。
"""

import json
import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider  # noqa: E402
from ragspine.service.api.app import create_app  # noqa: E402
from ragspine.service.config import ServiceConfig  # noqa: E402
from ragspine.service.tasks.task_queue import FakeQueue  # noqa: E402
from ragspine.workflows.formats import parse_workflow  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_client(
    tmp_path, *, run_enabled=False, timeout_s=10.0, provider=None, isolation="inprocess"
):
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=run_enabled,
        dify_run_timeout_s=timeout_s,
        dify_run_isolation=isolation,
    )
    app = create_app(config, provider=provider or MockProvider(), queue=FakeQueue())
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
    bad = (
        'app:\n  mode: chat\n  name: x\nkind: app\nversion: "0.1.5"\n'
        "workflow:\n  graph:\n    nodes: []\n    edges: []\n"
    )
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


def test_dify_compile_accepts_canonical_workflow_json(client):
    workflow = parse_workflow(_fixture("seq.yml"), format="yaml")

    resp = client.post("/v1/dify/compile", json={"workflow": workflow})

    assert resp.status_code == 200
    assert "def run_workflow(" in resp.json()["code"]


def test_dify_compile_never_reads_existing_path_string(
    client,
    tmp_path,
    monkeypatch,
):
    """HTTP yaml 字段永远是正文；同名服务端文件不得被读取或泄露。"""
    sentinel = "SERVER_LOCAL_SECRET_MUST_NOT_LEAK"
    path = tmp_path / "server-local.yml"
    path.write_text(
        _fixture("seq.yml") + f"\nserver_local_secret: {sentinel}\n",
        encoding="utf-8",
    )

    from ragspine.workflows import formats

    def reject_file_read(*args, **kwargs):
        raise AssertionError("HTTP str body must not read a server-side file")

    monkeypatch.setattr(formats, "read_bounded_file", reject_file_read)

    resp = client.post("/v1/dify/compile", json={"yaml": str(path)})

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "workflow.format"
    assert sentinel not in resp.text


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
    assert err["type"] == "workflow.format"


def test_dify_compile_bad_target_is_422_without_reflection(client):
    secret = "sk-secret-target-must-not-reflect"
    resp = client.post(
        "/v1/dify/compile",
        json={"yaml": _fixture("seq.yml"), "target": secret},
    )
    assert resp.status_code == 422
    assert secret not in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/v1/dify/analyze",
        "/v1/dify/compile",
        "/v1/dify/run",
        "/v1/dify/run/jobs",
    ],
)
def test_dify_canonical_document_over_one_mib_is_safe_400(tmp_path, path):
    secret = "sk-oversized-document-must-not-reflect"
    workflow = parse_workflow(_fixture("seq.yml"), format="yaml")
    # Each item stays below the per-string cap while their compact canonical JSON
    # representation exceeds the shared 1 MiB decoded-document limit.
    workflow["padding"] = [secret + ("x" * 249_900) for _ in range(5)]
    client = _make_client(tmp_path, run_enabled=True)

    resp = client.post(path, json={"workflow": workflow})

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "workflow.format"
    assert secret not in resp.text


def test_dify_canonical_document_over_node_limit_is_safe_400(client):
    workflow = parse_workflow(_fixture("seq.yml"), format="yaml")
    workflow["padding"] = [0] * 20_001

    resp = client.post("/v1/dify/compile", json={"workflow": workflow})

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "workflow.format"


def test_dify_legacy_yaml_document_uses_same_one_mib_byte_limit(client):
    secret = "sk-oversized-yaml-must-not-reflect"
    yaml_text = _fixture("seq.yml") + "\npadding: |\n  " + secret + ("x" * (1024 * 1024))

    resp = client.post("/v1/dify/compile", json={"yaml": yaml_text})

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "workflow.format"
    assert secret not in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/v1/workflow-scaffold",
        "/v1/dify/analyze",
        "/v1/dify/compile",
        "/v1/dify/run",
        "/v1/dify/run/jobs",
    ],
)
def test_workflow_json_routes_reject_oversized_raw_body_before_validation(
    client,
    path,
):
    secret = "sk-raw-body-must-not-reflect"
    raw = json.dumps(
        {"unexpected": secret + ("x" * (2 * 1024 * 1024))},
    ).encode()

    resp = client.post(
        path,
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "RequestTooLarge"
    assert secret not in resp.text


def test_workflow_json_route_rejects_chunked_oversized_raw_body(client):
    secret = b"sk-chunked-body-must-not-reflect"

    def chunks():
        yield b'{"yaml":"' + secret
        for _ in range(5):
            yield b"x" * (512 * 1024)

    resp = client.post(
        "/v1/dify/compile",
        content=chunks(),
        headers={
            "content-type": "application/json",
            "transfer-encoding": "chunked",
        },
    )

    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "RequestTooLarge"
    assert secret.decode() not in resp.text


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
    resp = client.post("/v1/dify/run", json={"yaml": _fixture("agent_tool.yml"), "inputs": {}})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["type"] == "dify.unsafe"
    # 断言确实没执行：骨架钩子若被 exec 会抛 NotImplementedError（dify.run_error/500），
    # 这里拿到的是 422 的 dify.unsafe，证明拒在 exec 之前。
    assert "NotImplementedError" in err["message"] or "骨架" in err["message"]


def test_dify_run_import_allowlist_via_unsupported_node(tmp_path):
    # list-operator 等未建模节点生成 NotImplementedError 骨架 + warning -> L0 闸 422（未执行）。
    # 这条间接验证「越权能力的节点不会被跑」——与单元层的 import 白名单互补。
    # （P9 后 http-request 已真实建模，改用仍未建模的 list-operator 保持覆盖面。）
    unsupported_yaml = (
        'app:\n  mode: workflow\n  name: x\nkind: app\nversion: "0.1.5"\n'
        "workflow:\n  graph:\n    nodes:\n"
        "      - id: start_1\n        data: {type: start, title: s, variables: []}\n"
        "      - id: lo_1\n        data: {type: list-operator, title: l}\n"
        "      - id: end_1\n        data: {type: end, title: e, outputs: []}\n"
        "    edges:\n"
        "      - {source: start_1, target: lo_1, sourceHandle: source}\n"
        "      - {source: lo_1, target: end_1, sourceHandle: source}\n"
    )
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": unsupported_yaml, "inputs": {}})
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "dify.unsafe"


# ---------------------------------------------------------------------------
# HTTP GATE — http-request 节点安全默认关（RAGSPINE_DIFY_HTTP_ENABLED）
# ---------------------------------------------------------------------------
HTTP_NODE_YAML = (
    'app:\n  mode: workflow\n  name: x\nkind: app\nversion: "0.1.5"\n'
    "workflow:\n  graph:\n    nodes:\n"
    "      - id: start_1\n        data: {type: start, title: s, variables: []}\n"
    "      - id: http_1\n"
    "        data: {type: http-request, title: h, method: get, url: 'http://example.com/api'}\n"
    "      - id: end_1\n"
    "        data: {type: end, title: e, outputs: [{variable: body, value_selector: [http_1, body]}]}\n"
    "    edges:\n"
    "      - {source: start_1, target: http_1, sourceHandle: source}\n"
    "      - {source: http_1, target: end_1, sourceHandle: source}\n"
)


def test_dify_run_http_node_rejected_when_http_disabled(tmp_path, monkeypatch):
    # http-request 已真实建模（无 warning），但 HTTP 出网默认关：L0 闸 3 拒 -> 422（未执行）。
    monkeypatch.delenv("RAGSPINE_DIFY_HTTP_ENABLED", raising=False)
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": HTTP_NODE_YAML, "inputs": {}})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["type"] == "dify.unsafe"
    # 错误信息明确说明开启方式。
    assert "RAGSPINE_DIFY_HTTP_ENABLED" in err["message"]


def test_dify_run_http_node_executes_when_enabled_with_controlled_client(
    tmp_path, monkeypatch
):
    # 显式开启 + 假客户端（测试禁止真实网络）：runner 注入生效，端到端 200。
    monkeypatch.setenv("RAGSPINE_DIFY_HTTP_ENABLED", "true")
    from ragspine.service.dify import runner as dify_runner

    def fake_build_http_client():
        def _client(request):
            assert request["method"] == "get"
            assert request["url"] == "http://example.com/api"
            return {"status_code": 200, "body": "pong", "headers": {"X-Fake": "1"}}

        return _client

    monkeypatch.setattr(dify_runner, "build_http_client", fake_build_http_client)
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": HTTP_NODE_YAML, "inputs": {}})
    assert resp.status_code == 200
    assert resp.json()["result"]["body"] == "pong"


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
    assert resp.json()["error"]["type"] == "workflow.format"


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
    # 请求体严格拒绝 provider_expr，且 422 不反射被拒绝值。
    resp2 = client.post(
        "/v1/dify/run",
        json={
            "yaml": _fixture("seq.yml"),
            "inputs": {"question": "hi"},
            "provider_expr": "__import__('os')",
        },
    )
    assert resp2.status_code == 422
    assert "__import__" not in resp2.text


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
    resp = client.post("/v1/dify/run/jobs", json={"yaml": _fixture("agent_tool.yml"), "inputs": {}})
    # 入队前 L0 闸拒 -> 422（绝不入队、绝不执行）
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "dify.unsafe"


def test_dify_run_async_compile_error_before_enqueue(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run/jobs", json={"yaml": ": : bad : [", "inputs": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "workflow.format"


# ---------------------------------------------------------------------------
# NODE TRACES — run 响应带节点级 execution trace（NodeTrace 契约）
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


def test_dify_run_returns_node_traces(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run", json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    # result 内不残留内部键。
    assert "__node_traces__" not in body["result"]
    traces = body["node_traces"]
    assert isinstance(traces, list)
    assert [t["node_id"] for t in traces] == ["start_1", "llm_1", "tt_1", "end_1"]
    # 契约逐字段断言（前端按此消费，字段名定死）。
    llm = traces[1]
    assert llm["index"] == 1
    assert llm["node_id"] == "llm_1"
    assert llm["title"] == "应答模型"
    assert llm["node_type"] == "llm"
    assert llm["status"] == "succeeded"
    assert isinstance(llm["elapsed_ms"], float)
    assert llm["elapsed_ms"] >= 0.0
    assert llm["inputs"] == {"start_1.question": "hi"}
    assert isinstance(llm["outputs"], dict) and "text" in llm["outputs"]
    assert llm["error"] is None


def test_dify_run_failure_response_includes_node_traces(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post("/v1/dify/run", json={"yaml": FAIL_TRACE_YAML, "inputs": {"question": "q"}})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "dify.run_error"
    # 固化形状：失败 trace 附在 error dict 的 node_traces 键（向后兼容新增）。
    traces = err["node_traces"]
    assert isinstance(traces, list) and traces
    failed = [t for t in traces if t["status"] == "failed"]
    assert failed and failed[0]["node_id"] == "code_1"
    assert "ValueError" in failed[0]["error"]


def test_dify_run_async_job_result_has_node_traces(tmp_path):
    client = _make_client(tmp_path, run_enabled=True)
    resp = client.post(
        "/v1/dify/run/jobs",
        json={"yaml": _fixture("seq.yml"), "inputs": {"question": "hi"}},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    status = client.get(f"/v1/jobs/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "finished"
    job_result = body["result"]
    # node_traces 与 result / warnings 平级。
    traces = job_result["node_traces"]
    assert isinstance(traces, list) and traces
    assert traces[0]["node_id"] == "start_1"
    assert "__node_traces__" not in job_result["result"]


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
