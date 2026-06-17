"""服务层端到端集成：HTTP 提交 ingestion job → 真 FakeQueue 按 func_path 执行 jobs.py。

Wave-2 两个模块（api routes 与 tasks/jobs）此前各自单测：api 用「只记录不执行」的
假队列，jobs 直接调函数。本测试补上二者之间的真实接缝——用会内联执行 job 的
FakeQueue 驱动 /v1/ingest/structured/jobs，证明 routes.py 里的 func_path 字面量能
被解析并跑通 jobs.run_structured_ingest_job，且 /v1/jobs/{id} 能读回完成态报告。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from fastapi.testclient import TestClient

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import JOB_FINISHED, FakeQueue


@pytest.fixture
def client(tmp_path, excel_fixture_path):
    """注入真 FakeQueue（内联执行 job）+ MockProvider + 空 FAQ 的 TestClient。

    allowed_upload_root 设为合成 xlsx 所在目录，使 ingest 路径校验放行该 fixture。
    """
    config = ServiceConfig(
        db_path=str(tmp_path / "facts.db"),
        mapping_db_path=str(tmp_path / "mapping.db"),
        queue_db_path=str(tmp_path / "review.db"),
        allowed_upload_root=str(excel_fixture_path.parent),
    )
    app = create_app(
        config, provider=MockProvider(), queue=FakeQueue(), faq_cache=FAQCache.empty()
    )
    return TestClient(app), config


def test_structured_ingest_job_runs_through_routes_to_jobs(client, excel_fixture_path):
    """提交 → FakeQueue 内联跑 jobs → 查状态：完成态且真实写入了事实。"""
    test_client, config = client

    submit = test_client.post(
        "/v1/ingest/structured/jobs", json={"file": str(excel_fixture_path)}
    )
    assert submit.status_code == 200
    job_id = submit.json()["job_id"]
    assert job_id

    status = test_client.get(f"/v1/jobs/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == JOB_FINISHED
    # jobs.run_structured_ingest_job 真实执行：报告里有写入的事实，且不含原始数值键
    assert body["result"]["status"] == "ok"
    assert body["result"]["n_facts_ingested"] > 0
    assert "value" not in body["result"]


def test_ask_endpoint_end_to_end_mock_provider(client):
    """/v1/ask 走 mock provider 全链路：返回结构化 JSON（含 request_id / route / cache）。"""
    test_client, _ = client
    resp = test_client.post("/v1/ask", json={"question": "香港FY2025的REVENUE是多少"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    assert body["route"] in {"structured", "narrative", "composite", "faq"}
    assert body["cache"]["hit"] is False
    assert body["answer_kind"] in {"normal", "clarification", "refusal"}
