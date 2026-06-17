"""健康检查接口测试：/healthz 进程存活；/readyz 依赖（fact db + queue.ping）。"""

import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.storage.fact_store import FactStore


class PingQueue:
    """最小队列桩：可控 ping 结果。"""

    def __init__(self, ping_ok=True):
        self._ping_ok = ping_ok

    def enqueue(self, *a, **k):  # pragma: no cover - 健康检查用不到
        raise AssertionError("not used")

    def get(self, job_id):  # pragma: no cover
        return None

    def ping(self) -> bool:
        return self._ping_ok


@pytest.fixture
def seeded_db_path(tmp_path):
    db_path = tmp_path / "fact_metric.db"
    fs = FactStore(db_path)
    fs.init_schema()
    fs.close()
    return db_path


def make_client(db_path, *, queue):
    config = ServiceConfig(db_path=str(db_path))
    app = create_app(
        config, provider=MockProvider(), queue=queue, faq_cache=FAQCache.empty()
    )
    return TestClient(app)


def test_healthz_ok(seeded_db_path):
    client = make_client(seeded_db_path, queue=PingQueue(True))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ready_when_all_good(seeded_db_path):
    client = make_client(seeded_db_path, queue=PingQueue(True))
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["fact_db"] is True
    assert body["checks"]["queue"] is True


def test_readyz_degraded_when_queue_down(seeded_db_path):
    client = make_client(seeded_db_path, queue=PingQueue(False))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["queue"] is False


def test_readyz_does_not_leak_paths(seeded_db_path):
    import json

    client = make_client(seeded_db_path, queue=PingQueue(True))
    body = client.get("/readyz").json()
    # checks 值只能是布尔，不泄露真实路径/密钥
    for v in body["checks"].values():
        assert isinstance(v, bool)
    assert str(seeded_db_path) not in json.dumps(body, ensure_ascii=False)
