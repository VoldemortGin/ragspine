"""ADR 0036 PR-2:store 接上 sqlite 后端后的端到端保证。

- 两后端逻辑字节相等:同一 PDF 在 files 与 sqlite 下产出同一个 ``store_digest``
  (逻辑名 → 字节)与同一个发布 id;
- 旧数据兼容:三代文件布局的目录在 sqlite 后端下重跑 → 零模型调用、零修复、同发布 id,
  再写入只进 db(外置 PDF 除外);旧代码(files 钉死)读 sqlite 目录走版本门,从不静默错读;
- 崩溃与回退:页事务中途被杀 → 未提交的页不可见、db 完好、重跑从模型缓存零调用恢复;
  db 写坏 → 重建 + ``storage_repairs.store_db``;探测失败 → ``auto`` 回退 files、
  显式 ``sqlite`` 报错;外来写者租约 → 该文档 ``failed``(原因码 ``store_busy``);
- ADR 0034(sqlite 分支):db 行每次 sweep 真读,篡改 db 行在发布 / 挂载前即拒绝;
  外置文件仍享 stat 凭据,但被改动就重读并拒绝。
"""

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.draft_publication import publish_draft
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.file_placement import sharded_path
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.probe import ProbeResult, clear_probe_cache
from ragspine.common.evidence.object_backend.protocol import BackendUnavailable, ClaimOwner
from ragspine.common.evidence.object_backend.sqlite import WRITER_CLAIM_FORMAT
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    FULL_STORE_DIGEST,
    FULL_TASKS,
    lite_env,
    mixed_folder,
    run_mode,
    sharded_layout_only,
    store_digest,
)
from tests.enterprise_pdf_rag.adapters.store_tamper_helpers import tamper_object

pytestmark = pytest.mark.usefixtures("_fresh_settings")


@pytest.fixture
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _select(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", kind)
    get_settings.cache_clear()


def _run(tmp_path: Path, root: str = "ingestion") -> FolderPipelineResult:
    return run_mode(tmp_path, "full", root=root)


def _document_dirs(tmp_path: Path, root: str = "ingestion") -> list[Path]:
    return sorted(p for p in (tmp_path / root).iterdir() if p.is_dir())


# ---- 两后端逻辑字节相等 ---------------------------------------------------------------


def test_sqlite_run_produces_the_same_logical_bytes_and_published_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    (document,) = _run(tmp_path).documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert document.object_backend == "sqlite"
    assert tasks == FULL_TASKS
    # 逻辑名 → 字节的摘要与请求指纹集,与 files 后端的钉死值相同;逻辑文件集则与同一
    # 入口的 files 运行逐名相等(``test_lite_ingest`` 另钉死裸管线入口的 543 个逻辑文件
    # 在两后端下同值)。
    digest, count, requests = store_digest(tmp_path / "ingestion")
    assert (digest, requests) == (FULL_STORE_DIGEST, FULL_REQUESTS_DIGEST)
    _select(monkeypatch, "files")
    tasks.clear()
    mixed_folder(tmp_path / "files-arm")
    _run(tmp_path / "files-arm")
    assert store_digest(tmp_path / "files-arm" / "ingestion") == (digest, count, requests)
    assert sharded_layout_only(tmp_path / "ingestion")
    # 平铺写入为零:db 行之外只有外置对象(PDF)与模型缓存(PR-3 起在 model-cache.sqlite)。
    (document_dir,) = _document_dirs(tmp_path)
    sharded = [
        path.relative_to(document_dir).as_posix()
        for path in document_dir.rglob("*")
        if path.is_file() and "-sharded/" in path.relative_to(document_dir).as_posix()
    ]
    assert sharded == [f"source/objects/sha256-sharded/{document_dir.name[:2]}/{document_dir.name}"]


# ---- 旧数据兼容 -------------------------------------------------------------------------


def _to_amendment_1(ingestion_root: Path) -> None:
    """把每个内联产物剪回对象(f577170 代:信封内联、产物是对象)。"""
    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
    from enterprise_pdf_rag.adapters.http.processing_schemas import StageEnvelope

    for pointer in sorted(ingestion_root.rglob("stage-cache-sharded/*/*")):
        head, envelope, output = [*pointer.read_bytes().split(b"\n", 2), b"", b""][:3]
        if not output:
            continue
        artifact = StageEnvelope.model_validate_json(envelope).outcome.artifact
        assert artifact is not None
        store = LocalDocumentStore(pointer.parents[2], activate_on_publish=False)
        assert store.put(output, media_type=artifact.media_type) == artifact
        pointer.write_bytes(head + b"\n" + envelope + b"\n")


def _to_envelope_objects(ingestion_root: Path) -> None:
    """再把信封剪回对象(3414e0c 代:指针只有摘要行,信封是对象)。"""
    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore

    _to_amendment_1(ingestion_root)
    for pointer in sorted(ingestion_root.rglob("stage-cache-sharded/*/*")):
        head, envelope = [*pointer.read_bytes().split(b"\n", 1), b""][:2]
        envelope = envelope.rstrip(b"\n")
        if not envelope:
            continue
        store = LocalDocumentStore(pointer.parents[2], activate_on_publish=False)
        store.put(envelope, media_type="application/json")
        pointer.write_bytes(head + b"\n")


def _to_flat(ingestion_root: Path) -> None:
    """最老的一代(b989625):全部平铺目录。"""
    import shutil

    _to_envelope_objects(ingestion_root)
    for sharded in sorted(ingestion_root.rglob("*-sharded")):
        flat = sharded.with_name(sharded.name.removesuffix("-sharded"))
        flat.mkdir(exist_ok=True)
        for shard in sharded.iterdir():
            for path in shard.iterdir():
                path.rename(flat / path.name)
        shutil.rmtree(sharded)


@pytest.mark.parametrize(
    "convert",
    [lambda _root: None, _to_amendment_1, _to_envelope_objects, _to_flat],
    ids=["inline-output", "amendment-1", "envelope-objects", "flat"],
)
def test_every_files_generation_reruns_under_sqlite_with_no_call_and_no_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, convert: object
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "files")
    _run(tmp_path)
    assert callable(convert)
    convert(tmp_path / "ingestion")
    tasks.clear()
    _select(monkeypatch, "sqlite")

    (document,) = _run(tmp_path).documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (sum(tasks.values()), document.live_calls, document.storage_repairs) == (0, 0, {})
    # 三代 + db 混存:scan 与 mount 照常。
    catalog = scan_catalog(tmp_path / "ingestion")
    (entry,) = catalog.documents
    assert entry.retrieval_status == "ready" and entry.object_backend == "sqlite"
    mounted = mount_document(entry, embedder=None)
    assert mounted.manifest().scope.source_sha256 == entry.document_id
    assert mounted.member_texts()


def test_a_second_pdf_into_a_files_generation_root_writes_rows_plus_external_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import mixed_pdf

    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "files")
    _run(tmp_path)
    first = {p for p in (tmp_path / "ingestion").rglob("*") if p.is_file()}
    # 第二份 PDF(不同字节、不同 sha):只应产生 db、外置 PDF 与模型缓存文件。
    mixed_pdf(tmp_path / "pdfs" / "second.pdf", kinds=("text", "chart"))
    _select(monkeypatch, "sqlite")

    result = _run(tmp_path)

    assert {document.status for document in result.documents} == {"published"}
    new_files = sorted(
        path.relative_to(tmp_path / "ingestion").as_posix()
        for path in (tmp_path / "ingestion").rglob("*")
        if path.is_file() and path not in first
    )
    digest = sha256((tmp_path / "pdfs" / "second.pdf").read_bytes()).hexdigest()
    # 新增文件只许是:db(第一份文档的重跑也只新增它的 db)、模型缓存(model-cache.sqlite)、
    # 每次运行都会写的 runs/ 诊断,与第二份 PDF 的外置原件;绝无新的分层小对象或
    # stage-cache 指针文件。
    review_export = f"{digest}/source/"  # full 模式的审阅导出(source.pdf / text.json 等)
    unexpected = [
        name
        for name in new_files
        if "store.sqlite" not in name
        and "/model-cache/" not in name
        and "/runs/" not in name
        and name != f"{digest}/source/objects/sha256-sharded/{digest[:2]}/{digest}"
        and not (
            name.startswith(review_export) and "/objects/" not in name and "stage-cache" not in name
        )
    ]
    assert unexpected == []
    assert (tmp_path / "ingestion" / digest / "processing" / "store.sqlite").is_file()
    assert not (tmp_path / "ingestion" / digest / "processing" / "stage-cache-sharded").exists()


def test_files_pinned_code_meets_a_sqlite_store_at_a_version_gate_not_a_misread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧代码(files 钉死)读 sqlite 写的目录:看不到发布指针(unpublished),重跑则重算
    stage;模型缓存也在 db 里(PR-3),旧代码同样读不到,于是重发每个调用(ADR 0036
    "Weaker":回滚到无后端版本会重算并可能重发模型调用)→ 同发布 id,绝不静默错读。"""
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    _run(tmp_path)
    tasks.clear()
    _select(monkeypatch, "files")

    catalog = scan_catalog(tmp_path / "ingestion")
    assert catalog.documents == ()
    assert len(catalog.unpublished) == 1

    (document,) = _run(tmp_path).documents
    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert tasks == FULL_TASKS
    assert document.live_calls == sum(FULL_TASKS.values())


# ---- 崩溃与回退 -------------------------------------------------------------------------

_CRASH_CHILD = """
import os, sys
from pathlib import Path
sys.path.insert(0, {src!r})
os.environ["APP_ROOT_DIR"] = {root!r}
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend
import hashlib
root = Path({store!r})
backend = SqliteBackend(root)
data_committed = b'{{"committed": true}}'
backend.put_object(hashlib.sha256(data_committed).hexdigest(), data_committed, "application/json")
with backend.transaction():
    data_lost = b'{{"lost": true}}'
    backend.put_object(hashlib.sha256(data_lost).hexdigest(), data_lost, "application/json")
    os._exit(1)
"""


def test_a_crash_inside_a_page_transaction_loses_only_that_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ROOT_DIR", str(tmp_path))
    store = tmp_path / "store"
    src = str(Path(__file__).parents[3] / "src")
    script = _CRASH_CHILD.format(src=src, root=str(tmp_path), store=str(store))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr

    from ragspine.common.evidence.object_backend.sqlite import SqliteBackend

    backend = SqliteBackend(store)
    try:
        committed = sha256(b'{"committed": true}').hexdigest()
        lost = sha256(b'{"lost": true}').hexdigest()
        assert backend.get_object(committed) == b'{"committed": true}'
        assert backend.get_object(lost) is None  # 未提交的页回滚,不是半个条目
        with closing(sqlite3.connect(store / "store.sqlite")) as connection:
            assert connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        # 崩溃持有者的写者租约按 pid 规则被接管:同 db 再写不被自己档住。
        assert backend.put_object(lost, b'{"lost": true}', "application/json") == "placed"
    finally:
        backend.close()


def test_a_corrupt_db_is_rebuilt_and_the_rerun_recovers_from_the_model_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    _run(tmp_path)
    (document_dir,) = _document_dirs(tmp_path)
    db = document_dir / "processing" / "store.sqlite"
    db.write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
    for sidecar in ("-wal", "-shm"):
        Path(str(db) + sidecar).unlink(missing_ok=True)
    tasks.clear()

    (document,) = _run(tmp_path).documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (sum(tasks.values()), document.live_calls) == (0, 0)  # 模型缓存在另一个 db,零真实调用
    assert document.storage_repairs.get("store_db", 0) >= 1
    assert list(document_dir.glob("processing/store.sqlite.corrupt-*"))


_WAL_CHILD = """
import hashlib, os, sys
from pathlib import Path
sys.path.insert(0, {src!r})
os.environ["APP_ROOT_DIR"] = {root!r}
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend
backend = SqliteBackend(Path({store!r}), synchronous="NORMAL")
for i in range(8):
    data = ('{{"n": %d}}' % i).encode()
    backend.put_object(hashlib.sha256(data).hexdigest(), data, "application/json")
os._exit(0)  # 崩溃语义:不 close、不 checkpoint,全部事务还躺在热 WAL 里
"""


def test_wal_tail_truncation_loses_at_most_the_last_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragspine.common.evidence.object_backend.sqlite import SqliteBackend

    monkeypatch.setenv("APP_ROOT_DIR", str(tmp_path))
    store = tmp_path / "store"
    src = str(Path(__file__).parents[3] / "src")
    script = _WAL_CHILD.format(src=src, root=str(tmp_path), store=str(store))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payloads = [f'{{"n": {i}}}'.encode() for i in range(8)]
    wal = Path(str(store / "store.sqlite") + "-wal")
    assert wal.stat().st_size > 0
    # 截掉尾部若干字节(撕裂尾帧):WAL 帧带校验和,恢复只保留到最后一个完好的提交帧。
    wal.write_bytes(wal.read_bytes()[: max(32, wal.stat().st_size - 2048)])

    fresh = SqliteBackend(store)
    try:
        recovered = [fresh.get_object(sha256(data).hexdigest()) is not None for data in payloads]
        # 一个前缀被恢复,最后的事务丢了;丢的由下一次携带字节的写补上。
        assert recovered == sorted(recovered, reverse=True)
        assert not all(recovered)
        lost = payloads[recovered.index(False)]
        assert fresh.put_object(sha256(lost).hexdigest(), lost, "application/json") == "placed"
        assert fresh.get_object(sha256(lost).hexdigest()) == lost
    finally:
        fresh.close()


def test_auto_falls_back_to_files_when_the_probe_fails_and_explicit_sqlite_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    clear_probe_cache()
    failed = ProbeResult(False, "sqlite_commit")
    monkeypatch.setattr(
        "ragspine.common.evidence.object_backend.registry.probe_directory",
        lambda *_args, **_kwargs: failed,
    )
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.folder_pipeline.probe_directory",
        lambda *_args, **_kwargs: failed,
    )
    _select(monkeypatch, "auto")

    result = _run(tmp_path)

    assert result.object_backend == "files"
    (document,) = result.documents
    assert document.status == "published" and document.object_backend == "files"
    assert not list((tmp_path / "ingestion").rglob("store.sqlite"))

    _select(monkeypatch, "sqlite")
    with pytest.raises(BackendUnavailable, match="sqlite_commit"):
        _run(tmp_path, root="ingestion-sqlite")
    clear_probe_cache()


def test_a_foreign_live_writer_lease_fails_the_document_with_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    folder = mixed_folder(tmp_path)
    digest = sha256((folder / "mixed.pdf").read_bytes()).hexdigest()
    source_root = tmp_path / "ingestion" / digest / "source"
    source_root.mkdir(parents=True)
    # 一个还活着的外来持有者(同机、活 pid、别的进程 token)占着写者租约。
    owner = ClaimOwner(
        host=__import__("socket").gethostname(),
        pid=os.getpid(),
        process="someone-else",
        created_at=lease.wall_clock(),
        lease_seconds=3600,
    )
    (source_root / "store.sqlite.writer").write_bytes(
        lease.owner_payload(WRITER_CLAIM_FORMAT, owner)
    )
    _select(monkeypatch, "sqlite")

    (document,) = _run(tmp_path).documents

    assert document.status == "failed" and document.failed_stage == "ingest"
    assert document.error is not None and "store_busy" in document.error


# ---- ADR 0034 的 sqlite 分支 -----------------------------------------------------------


def _published_store(tmp_path: Path) -> tuple[Path, str]:
    (document_dir,) = _document_dirs(tmp_path)
    store = ProcessingStore(document_dir / "processing", record_receipts=False)
    try:
        current = store.current_id()
        assert current is not None
        return document_dir, current
    finally:
        store.close()


def test_a_tampered_db_row_is_refused_by_the_next_sweep_despite_any_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    _run(tmp_path)
    document_dir, current = _published_store(tmp_path)
    processing_root = document_dir / "processing"
    # 发布 id 本身就是一个 db 行里的对象(processing manifest 信封):篡改这一行。
    tamper_object(processing_root, current)

    fresh = ProcessingStore(processing_root)
    try:
        with pytest.raises(ValueError, match=r"digest mismatch|damaged pointer"):
            fresh.load(current)
    finally:
        fresh.close()
    # 发布前的真读同样拒绝(receipts 在 publish 路径本来就关闭)。
    with pytest.raises(ValueError, match=r"digest mismatch|damaged pointer"):
        publish_draft(
            source_store=document_dir / "source",
            processing_store=processing_root,
            processing_id=current,
        )


def test_a_touched_external_file_is_reread_and_a_forged_one_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    _run(tmp_path)
    document_dir, _current = _published_store(tmp_path)
    source_root = document_dir / "source"
    pdf = sharded_path(source_root / "objects" / "sha256", document_dir.name)
    assert pdf.is_file()

    # 外置文件被改动(stat 变了):凭据失效,真读拒绝伪造字节。
    pdf.write_bytes(b"%PDF-1.7 forged")
    store = ProcessingStore(document_dir / "processing")
    sources_root_store = __import__(
        "enterprise_pdf_rag.adapters.document_store", fromlist=["LocalDocumentStore"]
    ).LocalDocumentStore(source_root)
    try:
        manifest_id = sources_root_store.current_manifest_id()
        assert manifest_id is not None
        with pytest.raises(ValueError, match="digest mismatch"):
            sources_root_store.load(manifest_id)
    finally:
        sources_root_store.close()
        store.close()


def test_sqlite_receipts_let_a_fresh_instance_skip_rereading_the_external_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore

    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    _select(monkeypatch, "sqlite")
    _run(tmp_path)
    document_dir, _current = _published_store(tmp_path)
    source_root = document_dir / "source"
    pdf = sharded_path(source_root / "objects" / "sha256", document_dir.name)

    first = LocalDocumentStore(source_root, activate_on_publish=False)
    try:
        manifest_id = first.current_manifest_id()
        assert manifest_id is not None
        first.load(manifest_id)  # 全量 sweep,留下 sqlite 外置凭据(records 行)
    finally:
        first.close()
    with closing(sqlite3.connect(source_root / "store.sqlite")) as connection:
        row = connection.execute(
            "SELECT bytes FROM records WHERE name = ?",
            (f"verification-receipts/{manifest_id}",),
        ).fetchone()
    assert row is not None and b"verification-receipt-sqlite-v1" in bytes(row[0])

    reads: list[Path] = []
    real = Path.read_bytes

    def counted(path: Path) -> bytes:
        reads.append(path)
        return real(path)

    second = LocalDocumentStore(source_root, activate_on_publish=False)
    try:
        monkeypatch.setattr(Path, "read_bytes", counted)
        second.load(manifest_id)
    finally:
        monkeypatch.setattr(Path, "read_bytes", real)
        second.close()
    assert pdf not in reads  # 外置 PDF 由 stat 凭据放行;db 行照常真读
