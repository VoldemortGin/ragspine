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


@pytest.fixture
def client(tmp_path):
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    app = create_app(config, provider=MockProvider(), queue=FakeQueue())
    return TestClient(app)


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
