"""端到端：analyze -> compile -> run（同步 + 异步）一条链，TestClient + MockProvider。

冒烟整条 Dify 工作流服务化路径：先 analyze 拿优化建议、再 compile 拿可读代码、再
run 实际执行（开启 run_enabled）。零真实 LLM API。
"""

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


@pytest.fixture
def client(tmp_path):
    config = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        dify_run_enabled=True,
        dify_run_timeout_s=10.0,
    )
    app = create_app(config, provider=MockProvider(), queue=FakeQueue())
    return TestClient(app)


def test_analyze_then_compile_then_run_sync(client):
    yaml_text = (FIXTURES / "seq.yml").read_text(encoding="utf-8")

    # 1) analyze —— 只建议，零代码生成
    a = client.post("/v1/dify/analyze", json={"yaml": yaml_text})
    assert a.status_code == 200
    assert "suggestions" in a.json()

    # 2) compile —— 拿可读纯 Python 代码字符串（不执行）
    c = client.post("/v1/dify/compile", json={"yaml": yaml_text})
    assert c.status_code == 200
    code = c.json()["code"]
    assert "def run_workflow(" in code
    assert c.json()["warnings"] == []

    # 3) run —— 实际受限执行，返回结果
    r = client.post(
        "/v1/dify/run", json={"yaml": yaml_text, "inputs": {"question": "你好"}}
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert "result" in result
    assert isinstance(result["result"], str)


def test_compile_then_run_async(client):
    yaml_text = (FIXTURES / "qa_fold.yml").read_text(encoding="utf-8")

    # compile 先看一眼可读代码（含 answer_question 折叠）
    c = client.post("/v1/dify/compile", json={"yaml": yaml_text})
    assert c.status_code == 200
    assert "answer_question" in c.json()["code"]

    # 异步执行：入队 -> 轮询 GET /v1/jobs/{id}（FakeQueue 内联跑完）
    sub = client.post(
        "/v1/dify/run/jobs", json={"yaml": yaml_text, "inputs": {"question": "你好"}}
    )
    assert sub.status_code == 200
    job_id = sub.json()["job_id"]

    st = client.get(f"/v1/jobs/{job_id}")
    assert st.status_code == 200
    body = st.json()
    assert body["status"] == "finished"
    assert "result" in body["result"]


def test_run_disabled_path_is_safe(tmp_path):
    # 默认配置（run 未开）：analyze/compile 可用，run 一律 403——安全默认。
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))  # dify_run_enabled 默认 False
    client = TestClient(create_app(config, provider=MockProvider(), queue=FakeQueue()))
    yaml_text = (FIXTURES / "seq.yml").read_text(encoding="utf-8")

    assert client.post("/v1/dify/analyze", json={"yaml": yaml_text}).status_code == 200
    assert client.post("/v1/dify/compile", json={"yaml": yaml_text}).status_code == 200
    run = client.post(
        "/v1/dify/run", json={"yaml": yaml_text, "inputs": {"question": "x"}}
    )
    assert run.status_code == 403
    assert run.json()["error"]["type"] == "dify.run_disabled"
