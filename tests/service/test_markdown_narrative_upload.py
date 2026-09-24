"""服务端叙事入库接受 .md（HTTP 路由 + worker），并守住 allowed_upload_root。

- .md 与 .pptx/.pdf 一样过 validate_ingest_path（resolve 后必须在允许根目录内 + 后缀白名单）；
- 关联的 source PDF（sidecar / payload）也必须在允许根目录内，越界在写入前按 validation 失败；
- 没有内容嗅探：伪装成 .md 的二进制按 UTF-8（errors='replace'）当文本解析，绝不执行；
- 其他后缀仍被拒；结构化通道不收 .md。
"""

import json
import os
import sys
from pathlib import Path

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.ingestion.page_images.source_pdf import sidecar_path
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.jobs import run_narrative_ingest_job, run_structured_ingest_job
from ragspine.service.tasks.task_queue import JOB_FAILED, JOB_FINISHED, FakeQueue, JobError
from ragspine.storage.fact_store import SqliteFactStore
from tests.ingestion.page_images.fixtures import make_pdf, write_deck

_PAGES = ["Agency channel mix overview.", "Partnership channel outlook.", "Closing remarks."]


@pytest.fixture
def upload_root(tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture
def config(tmp_path: Path, upload_root: Path) -> ServiceConfig:
    db = tmp_path / "fact.db"
    store = SqliteFactStore(db)
    store.init_schema()
    store.close()
    return ServiceConfig(
        db_path=str(db),
        chunk_db_path=str(tmp_path / "chunks.db"),
        mapping_db_path=str(tmp_path / "mapping.db"),
        queue_db_path=str(tmp_path / "review.db"),
        allowed_upload_root=str(upload_root),
        page_image_dpi=72,
    )


def _client(config: ServiceConfig, queue: FakeQueue) -> TestClient:
    app = create_app(config, provider=MockProvider(), queue=queue, faq_cache=FAQCache.empty())
    return TestClient(app)


def _submit(config: ServiceConfig, inputs: list[str]) -> tuple[int, FakeQueue, dict]:
    queue = FakeQueue()
    resp = _client(config, queue).post("/v1/ingest/narrative/jobs", json={"inputs": inputs})
    return resp.status_code, queue, resp.json()


def _doc_ids(chunk_db: str) -> set[str]:
    if not Path(chunk_db).exists():
        return set()
    store = ChunkStore(chunk_db)
    try:
        store.init_schema()
        return {r[0] for r in store.execute_read("SELECT DISTINCT doc_id FROM narrative_chunk")}
    finally:
        store.close()


def _worker_payload(config: ServiceConfig, inputs: list[str], **extra: object) -> dict:
    return {
        "inputs": inputs,
        "chunk_db_path": config.chunk_db_path,
        "allowed_upload_root": config.allowed_upload_root,
        **extra,
    }


# --- 放行：根目录内的 .md 走 HTTP → worker，sidecar 的 source_pdf 关联页图 -----------------------


def test_http_md_with_sidecar_pdf_runs_end_to_end(config, upload_root):
    md, pdf = write_deck(upload_root, _PAGES)
    sidecar_path(md).write_text(json.dumps({"source_pdf": pdf.name}), encoding="utf-8")

    status, queue, body = _submit(config, [str(md)])

    assert status == 200
    job = queue.get(body["job_id"])
    assert job is not None and job.status == JOB_FINISHED, job
    assert job.result["files"][0]["status"] == "ingested"
    assert job.result["page_images"]["docs"][0]["n_images"] == 3
    assert _doc_ids(config.chunk_db_path) == {"deck.md"}


def test_worker_md_with_explicit_source_pdf_inside_root(config, upload_root):
    md, pdf = write_deck(upload_root, _PAGES)
    result = run_narrative_ingest_job(_worker_payload(config, [str(md)], source_pdf=str(pdf)))
    assert result["files"][0]["status"] == "ingested"
    assert result["page_images"]["counts"]["rendered"] == 1


# --- 拒绝：.md 路径穿越 ------------------------------------------------------------------------


def test_http_rejects_md_path_traversal(config, upload_root, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("# Secret\n\nleaked\n", encoding="utf-8")
    traversal = str(upload_root / ".." / "secret.md")

    status, queue, body = _submit(config, [traversal])

    assert status == 400
    assert body["error"]["type"] == "PathNotAllowedError"
    assert "允许根目录" in body["error"]["message"]
    assert queue._jobs == {}
    assert _doc_ids(config.chunk_db_path) == set()


def test_worker_rejects_md_path_traversal(config, upload_root, tmp_path):
    (tmp_path / "secret.md").write_text("# Secret\n\nleaked\n", encoding="utf-8")
    payload = _worker_payload(config, [str(upload_root / ".." / "secret.md")])
    with pytest.raises(JobError) as info:
        run_narrative_ingest_job(payload)
    assert info.value.stage == "validation"
    assert "允许根目录" in str(info.value)
    assert _doc_ids(config.chunk_db_path) == set()


def test_http_rejects_md_symlink_escaping_root(config, upload_root, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("# Secret\n\nleaked\n", encoding="utf-8")
    link = upload_root / "innocent.md"
    try:
        link.symlink_to(outside)
    except OSError:  # Windows 无符号链接权限时跳过
        pytest.skip("symlinks not permitted on this platform")

    status, queue, body = _submit(config, [str(link)])

    assert status == 400
    assert "允许根目录" in body["error"]["message"]
    assert queue._jobs == {}


# --- 拒绝：source_pdf 越出 allowed_upload_root ------------------------------------------------


def test_http_md_sidecar_pdf_outside_root_fails_validation_before_writing(
    config, upload_root, tmp_path
):
    md, _ = write_deck(upload_root, _PAGES)
    outside_pdf = make_pdf(tmp_path / "outside.pdf", ["a", "b", "c"])
    sidecar_path(md).write_text(json.dumps({"source_pdf": str(outside_pdf)}), encoding="utf-8")

    status, queue, body = _submit(config, [str(md)])

    assert status == 200  # 路由只校验输入文件；关联 PDF 由 worker 在写入前校验
    job = queue.get(body["job_id"])
    assert job is not None and job.status == JOB_FAILED
    assert job.error is not None and job.error["stage"] == "validation"
    assert "allowed_upload_root" in job.error["message"]
    assert _doc_ids(config.chunk_db_path) == set()


def test_worker_md_sidecar_relative_traversal_pdf_is_rejected(config, upload_root, tmp_path):
    md, _ = write_deck(upload_root, _PAGES)
    make_pdf(tmp_path / "outside.pdf", ["a", "b", "c"])
    sidecar_path(md).write_text(json.dumps({"source_pdf": "../outside.pdf"}), encoding="utf-8")
    with pytest.raises(JobError) as info:
        run_narrative_ingest_job(_worker_payload(config, [str(md)]))
    assert info.value.stage == "validation"
    assert "allowed_upload_root" in str(info.value)
    assert _doc_ids(config.chunk_db_path) == set()


def test_worker_md_explicit_pdf_outside_root_is_rejected(config, upload_root, tmp_path):
    md, _ = write_deck(upload_root, _PAGES)
    outside_pdf = make_pdf(tmp_path / "outside.pdf", ["a", "b", "c"])
    with pytest.raises(JobError) as info:
        run_narrative_ingest_job(_worker_payload(config, [str(md)], source_pdf=str(outside_pdf)))
    assert info.value.stage == "validation"
    assert "allowed_upload_root" in str(info.value)
    assert _doc_ids(config.chunk_db_path) == set()


# --- 伪装成 .md 的二进制：无内容嗅探，只当文本解析，绝不执行 -------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="exec bit / shebang are POSIX-only")
def test_binary_disguised_as_md_is_parsed_as_text_never_executed(config, upload_root, tmp_path):
    marker = tmp_path / "executed.marker"
    disguised = upload_root / "payload.md"
    disguised.write_bytes(
        f"#!/bin/sh\ntouch {marker}\n".encode()
        + b"\x7fELF\x02\x01\x01\x00\x00\x00\xff\xfe\x00\x80\x81binary tail\n"
    )
    disguised.chmod(0o755)

    status, queue, body = _submit(config, [str(disguised)])

    assert status == 200
    job = queue.get(body["job_id"])
    assert job is not None and job.status == JOB_FINISHED, job
    assert job.result["files"][0]["status"] in ("ingested", "no_text")
    assert not marker.exists(), "上传内容绝不能被执行"
    store = ChunkStore(config.chunk_db_path)
    try:
        texts = [
            r[0]
            for r in store.execute_read(
                "SELECT text FROM narrative_chunk WHERE doc_id = ?", ("payload.md",)
            )
        ]
    finally:
        store.close()
    assert all(isinstance(t, str) for t in texts)
    assert any("�" in t for t in texts), "非法 UTF-8 字节按 errors='replace' 解码为文本"


# --- 其他后缀仍被拒；结构化通道不收 .md ---------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["notes.txt", "memo.docx", "run.sh", "tool.exe", "doc.markdown", "doc.md.exe", "noext"]
)
def test_other_suffixes_still_rejected(config, upload_root, name):
    path = upload_root / name
    path.write_bytes(b"x")

    status, queue, _ = _submit(config, [str(path)])
    assert status == 400
    assert queue._jobs == {}

    with pytest.raises(JobError) as info:
        run_narrative_ingest_job(_worker_payload(config, [str(path)]))
    assert info.value.stage == "validation"


def test_structured_channel_still_rejects_md(config, upload_root):
    md, _ = write_deck(upload_root, _PAGES)
    queue = FakeQueue()
    resp = _client(config, queue).post("/v1/ingest/structured/jobs", json={"file": str(md)})
    assert resp.status_code == 400
    assert queue._jobs == {}

    with pytest.raises(JobError) as info:
        run_structured_ingest_job(
            {
                "file": str(md),
                "db_path": config.db_path,
                "mapping_db_path": config.mapping_db_path,
                "queue_db_path": config.queue_db_path,
                "manifest_db_path": None,
                "batch_id": None,
                "dry_run": False,
                "valid_as_of": None,
                "allowed_upload_root": config.allowed_upload_root,
            }
        )
    assert info.value.stage == "validation"
