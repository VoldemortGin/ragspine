"""worker job 函数（durable ingestion）测试。

只验证外部行为：worker 自开自闭 store、序列化 payload 进、纯 JSON report 出，
report 中绝不出现原始 fact 数值 / chunk 正文。TDD：先红后绿。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.service.tasks.jobs import (
    NARRATIVE_INGEST_JOB,
    STRUCTURED_INGEST_JOB,
    run_narrative_ingest_job,
    run_structured_ingest_job,
)
from ragspine.service.tasks.task_queue import (
    JOB_FINISHED,
    FakeQueue,
    JobError,
)
from ragspine.storage.fact_store import FactStore

# IngestReport 序列化后允许出现的 key 全集（绝不含 "value" 等原始事实数值）
_STRUCTURED_REPORT_KEYS = {
    "source_path",
    "source_doc_id",
    "file_hash",
    "dry_run",
    "n_grids",
    "n_facts_extracted",
    "n_facts_ingested",
    "n_tags_applied",
    "n_enqueued_review",
    "warnings",
    "status",
    "error",
}


def _structured_payload(tmp_path, excel_fixture_path, **overrides) -> dict:
    payload = {
        "file": str(excel_fixture_path),
        "db_path": str(tmp_path / "fact.db"),
        "mapping_db_path": str(tmp_path / "mapping.db"),
        "queue_db_path": str(tmp_path / "queue.db"),
        "manifest_db_path": None,
        "batch_id": None,
        "dry_run": False,
        "valid_as_of": None,
        "allowed_upload_root": None,
    }
    payload.update(overrides)
    return payload


def test_structured_job_ingests_real_facts(tmp_path, excel_fixture_path):
    payload = _structured_payload(tmp_path, excel_fixture_path)
    report = run_structured_ingest_job(payload)

    assert isinstance(report, dict)
    assert report["status"] == "ok"
    assert report["n_facts_ingested"] > 0


def test_structured_report_is_json_serializable_no_raw_values(tmp_path, excel_fixture_path):
    payload = _structured_payload(tmp_path, excel_fixture_path)
    report = run_structured_ingest_job(payload)

    # round-trips through JSON
    round_tripped = json.loads(json.dumps(report))
    assert round_tripped == report

    # 只暴露既定的 report key，绝无 "value" 等原始事实数值
    assert set(report.keys()) == _STRUCTURED_REPORT_KEYS
    assert "value" not in report


def test_structured_dry_run_writes_nothing(tmp_path, excel_fixture_path):
    payload = _structured_payload(tmp_path, excel_fixture_path, dry_run=True)
    report = run_structured_ingest_job(payload)

    assert report["dry_run"] is True
    assert report["n_facts_ingested"] == 0


def test_structured_job_owns_and_closes_its_store(tmp_path, excel_fixture_path):
    db_path = str(tmp_path / "fact.db")
    payload = _structured_payload(tmp_path, excel_fixture_path, db_path=db_path)
    report = run_structured_ingest_job(payload)
    assert report["n_facts_ingested"] > 0

    # 独立重新打开同一 db：facts 可查询，证明 worker 写入并正确关闭了连接
    store = FactStore(db_path)
    store.init_schema()
    try:
        assert store.count() == report["n_facts_ingested"]
    finally:
        store.close()


def test_structured_job_rejects_path_outside_allowed_root(tmp_path, excel_fixture_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    payload = _structured_payload(
        tmp_path, excel_fixture_path, allowed_upload_root=str(allowed)
    )
    with pytest.raises(JobError) as exc_info:
        run_structured_ingest_job(payload)
    assert exc_info.value.stage == "validation"


def test_structured_job_rejects_unsupported_suffix(tmp_path):
    bad = tmp_path / "note.txt"
    bad.write_text("not a spreadsheet", encoding="utf-8")
    payload = _structured_payload(tmp_path, bad, file=str(bad))
    with pytest.raises(JobError) as exc_info:
        run_structured_ingest_job(payload)
    assert exc_info.value.stage == "validation"


def test_narrative_job_returns_per_file_status_no_text_leak(tmp_path, styled_deck_path):
    chunk_db = str(tmp_path / "chunks.db")
    payload = {
        "inputs": [str(styled_deck_path)],
        "chunk_db_path": chunk_db,
        "meta_by_doc": None,
        "dry_run": False,
        "allowed_upload_root": None,
    }
    report = run_narrative_ingest_job(payload)

    # JSON 序列化 round-trip
    assert json.loads(json.dumps(report)) == report

    assert "counts" in report
    assert isinstance(report["files"], list)
    assert len(report["files"]) == 1
    file_rep = report["files"][0]
    assert file_rep["status"] in ("ingested", "skipped", "failed", "no_text")
    # 每个文件 dict 只暴露既定 key，绝无 chunk 正文 / text 字段
    assert set(file_rep.keys()) == {
        "path",
        "doc_id",
        "status",
        "n_chunks",
        "n_skipped_pages",
        "file_hash",
        "error",
        "warnings",
    }
    assert "text" not in file_rep
    assert "chunks" not in file_rep


def test_structured_job_via_fake_queue_end_to_end(tmp_path, excel_fixture_path):
    payload = _structured_payload(tmp_path, excel_fixture_path)
    queue = FakeQueue()
    job_id = queue.enqueue(STRUCTURED_INGEST_JOB, payload)

    status = queue.get(job_id)
    assert status is not None
    assert status.status == JOB_FINISHED
    # FakeQueue 内联执行的结果应与直接调用 job 函数一致
    assert status.result == run_structured_ingest_job(payload)


def test_narrative_job_via_fake_queue_end_to_end(tmp_path, styled_deck_path):
    payload = {
        "inputs": [str(styled_deck_path)],
        "chunk_db_path": str(tmp_path / "chunks.db"),
        "meta_by_doc": None,
        "dry_run": False,
        "allowed_upload_root": None,
    }
    queue = FakeQueue()
    job_id = queue.enqueue(NARRATIVE_INGEST_JOB, payload)

    status = queue.get(job_id)
    assert status is not None
    assert status.status == JOB_FINISHED
    assert status.result["files"][0]["status"] in (
        "ingested",
        "skipped",
        "failed",
        "no_text",
    )
