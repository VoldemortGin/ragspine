"""ingestion job 提交 + job 状态查询接口测试。

用 RecordingQueue 记录 enqueue（不真正执行 job），断言 payload 只含可序列化数据、
路径安全（allowed_upload_root + 后缀）、job 状态映射、未知 id -> 404。
"""

import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.api.routes import NARRATIVE_INGEST_JOB, STRUCTURED_INGEST_JOB
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import (
    JOB_FINISHED,
    JobStatus,
)
from ragspine.storage.fact_store import FactStore


class RecordingQueue:
    """记录 enqueue 调用、返回可注入 JobStatus 的假队列（不执行真任务）。"""

    def __init__(self):
        self.calls = []  # list[(func_path, payload, kwargs)]
        self._statuses: dict[str, JobStatus] = {}

    def enqueue(self, func_path, payload, *, job_id=None, timeout=None,
                max_retries=0, result_ttl=None, failure_ttl=None):
        self.calls.append((func_path, payload, {
            "job_id": job_id, "timeout": timeout, "max_retries": max_retries,
            "result_ttl": result_ttl, "failure_ttl": failure_ttl,
        }))
        jid = job_id or f"job-{len(self.calls)}"
        self._statuses.setdefault(
            jid, JobStatus(id=jid, status="queued", result=None, error=None)
        )
        return jid

    def get(self, job_id):
        return self._statuses.get(job_id)

    def set_status(self, status: JobStatus):
        self._statuses[status.id] = status

    def ping(self):  # pragma: no cover
        return True


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "fact_metric.db"
    fs = FactStore(p)
    fs.init_schema()
    fs.close()
    return p


@pytest.fixture
def upload_root(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture
def config(db_path, upload_root, tmp_path):
    return ServiceConfig(
        db_path=str(db_path),
        chunk_db_path=str(tmp_path / "chunks.db"),
        mapping_db_path=str(tmp_path / "mapping.db"),
        queue_db_path=str(tmp_path / "review.db"),
        manifest_db_path=str(tmp_path / "manifest.db"),
        allowed_upload_root=str(upload_root),
    )


def make_client(config, queue):
    app = create_app(
        config, provider=MockProvider(), queue=queue, faq_cache=FAQCache.empty()
    )
    return TestClient(app)


def _make_file(upload_root, name: str) -> str:
    p = upload_root / name
    p.write_bytes(b"x")
    return str(p)


# ---------------------------------------------------------------------------
# STRUCTURED ingestion job submission
# ---------------------------------------------------------------------------
def test_submit_structured_job_returns_job_id(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    file = _make_file(upload_root, "deck.xlsx")
    resp = client.post("/v1/ingest/structured/jobs", json={"file": file})
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body and body["job_id"]

    assert len(queue.calls) == 1
    func_path, payload, kwargs = queue.calls[0]
    assert func_path == STRUCTURED_INGEST_JOB
    # payload 只含可序列化标量/路径
    for v in payload.values():
        assert v is None or isinstance(v, (str, bool, int, float))
    assert payload["file"].endswith("deck.xlsx")
    assert payload["db_path"] == config.db_path
    assert payload["mapping_db_path"] == config.mapping_db_path
    assert payload["queue_db_path"] == config.queue_db_path
    assert payload["manifest_db_path"] == config.manifest_db_path
    assert payload["allowed_upload_root"] == config.allowed_upload_root
    assert payload["dry_run"] is False


def test_submit_structured_job_passes_options(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    file = _make_file(upload_root, "deck.pptx")
    resp = client.post("/v1/ingest/structured/jobs", json={
        "file": file, "dry_run": True, "valid_as_of": "2026-03-31",
        "batch_id": "B1", "job_id": "myjob",
    })
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "myjob"
    _, payload, kwargs = queue.calls[0]
    assert payload["dry_run"] is True
    assert payload["valid_as_of"] == "2026-03-31"
    assert payload["batch_id"] == "B1"
    assert kwargs["job_id"] == "myjob"


def test_submit_structured_job_rejects_outside_root(config, tmp_path):
    queue = RecordingQueue()
    client = make_client(config, queue)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"x")
    resp = client.post("/v1/ingest/structured/jobs", json={"file": str(outside)})
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert queue.calls == []


def test_submit_structured_job_rejects_bad_suffix(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    file = _make_file(upload_root, "notes.txt")
    resp = client.post("/v1/ingest/structured/jobs", json={"file": file})
    assert resp.status_code == 400
    assert queue.calls == []


# ---------------------------------------------------------------------------
# NARRATIVE ingestion job submission
# ---------------------------------------------------------------------------
def test_submit_narrative_job_returns_job_id(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    file = _make_file(upload_root, "report.pdf")
    resp = client.post("/v1/ingest/narrative/jobs", json={"inputs": [file]})
    assert resp.status_code == 200
    assert resp.json()["job_id"]
    func_path, payload, _ = queue.calls[0]
    assert func_path == NARRATIVE_INGEST_JOB
    assert payload["chunk_db_path"] == config.chunk_db_path
    assert payload["allowed_upload_root"] == config.allowed_upload_root
    assert payload["dry_run"] is False
    assert isinstance(payload["inputs"], list)
    assert payload["inputs"][0].endswith("report.pdf")


def test_submit_narrative_job_rejects_bad_suffix(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    bad = _make_file(upload_root, "report.xlsx")  # narrative 只收 pptx/pdf
    resp = client.post("/v1/ingest/narrative/jobs", json={"inputs": [bad]})
    assert resp.status_code == 400
    assert queue.calls == []


def test_submit_narrative_job_passes_meta(config, upload_root):
    queue = RecordingQueue()
    client = make_client(config, queue)
    file = _make_file(upload_root, "report.pptx")
    meta = {"report.pptx": {"topic": "regulatory"}}
    resp = client.post("/v1/ingest/narrative/jobs", json={
        "inputs": [file], "dry_run": True, "meta_by_doc": meta,
    })
    assert resp.status_code == 200
    _, payload, _ = queue.calls[0]
    assert payload["dry_run"] is True
    assert payload["meta_by_doc"] == meta


# ---------------------------------------------------------------------------
# JOB STATUS query
# ---------------------------------------------------------------------------
def test_get_job_status_returns_stored(config):
    queue = RecordingQueue()
    queue.set_status(JobStatus(
        id="abc", status=JOB_FINISHED,
        result={"status": "ok", "facts": 3, "warnings": []},
    ))
    client = make_client(config, queue)
    resp = client.get("/v1/jobs/abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "abc"
    assert body["status"] == "finished"
    assert body["result"] == {"status": "ok", "facts": 3, "warnings": []}
    assert body["error"] is None


def test_get_job_status_failed(config):
    queue = RecordingQueue()
    queue.set_status(JobStatus(
        id="bad", status="failed",
        error={"type": "JobError", "message": "boom", "stage": "execution",
               "retryable": False},
    ))
    client = make_client(config, queue)
    body = client.get("/v1/jobs/bad").json()
    assert body["status"] == "failed"
    assert body["error"]["type"] == "JobError"
    assert body["error"]["stage"] == "execution"


def test_get_unknown_job_returns_404(config):
    queue = RecordingQueue()
    client = make_client(config, queue)
    resp = client.get("/v1/jobs/nope")
    assert resp.status_code == 404
    assert "error" in resp.json()
