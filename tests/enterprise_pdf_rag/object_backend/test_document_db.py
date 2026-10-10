"""每份文档一个 db(enterprise-pdf-rag ADR 0047):staged 下 ``<doc>/source``、``<doc>/processing``
与 ``<doc>/processing/model-cache`` 解析到同一个 ``StagedDocument``,发布成 ``<doc>/document.sqlite``。

- 三个 scope 互不串读(对象成员表 / stage-cache / 指针 / records 各自一份;模型缓存表独此一份);
- 同 digest 的对象跨 scope 只存一行字节(``objects`` 共用),每个 scope 各记一条成员行;
- PDF 原件进库(内联到 64 MiB;显式的 ``APP_OBJECT_STORE_EXTERNAL_MEDIA_TYPES`` 仍优先),
  字节可取回且一致;它没有文件,``content_path`` 与内联对象一样是 ``LookupError``;
- 被杀 / 本地盘丢失 / 本地有未发布进度时都能续上,发布目录始终只有一个整文件;
- T8 的 4 文件布局(或 sqlite 后端的 ``store.sqlite``)→ ``layout_mismatch``;
  非 staged 模式打开一个只有 ``document.sqlite`` 的文档 → 同样 ``layout_mismatch``;
- 默认模式(``sqlite`` / ``files``)不认识文档 db:照旧每个 store 根一个 ``store.sqlite``。
"""

import hashlib
import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import Settings
from ragspine.common.evidence.object_backend import lease, staged
from ragspine.common.evidence.object_backend.files import FileBackend
from ragspine.common.evidence.object_backend.probe import clear_probe_cache
from ragspine.common.evidence.object_backend.protocol import (
    LayoutMismatch,
    ObjectBackend,
    StoreBusy,
)
from ragspine.common.evidence.object_backend.registry import open_backend
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend
from ragspine.common.evidence.object_backend.staged import (
    PUBLISHER_CLAIM_FORMAT,
    STAGED_PDF_INLINE_MAX_BYTES,
    StagedDocument,
    StagedDocumentBackend,
    StagedDocumentModelCache,
    commit_staged,
    release_staged,
)
from tests.enterprise_pdf_rag.object_backend.conftest import live_owner, sha, stage_envelope

_KEY = "a" * 64
_RECORD = b'{"request_fingerprint": "' + b"a" * 64 + b'", "response_digest": null}'


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_probe_cache()
    yield
    release_staged(Path("/"))


def _settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        object_store_backend="staged",
        object_store_staging_dir=tmp_path / "staging",
        **extra,  # type: ignore[arg-type]
    )


def _doc(tmp_path: Path) -> Path:
    return (tmp_path / "ingestion" / ("d" * 64)).resolve()


def _open_all(
    tmp_path: Path, **extra: object
) -> tuple[ObjectBackend, ObjectBackend, StagedDocumentModelCache]:
    settings = _settings(tmp_path, **extra)
    source = open_backend(_doc(tmp_path) / "source", settings=settings)
    processing = open_backend(_doc(tmp_path) / "processing", settings=settings)
    cache = open_backend(
        _doc(tmp_path) / "processing" / "model-cache", "model-cache", settings=settings
    )
    assert isinstance(cache, StagedDocumentModelCache)
    return source, processing, cache


def _close(*backends: ObjectBackend) -> None:
    for backend in backends:
        backend.close()


def _put(backend: ObjectBackend, text: str, media_type: str = "text/plain") -> str:
    data = text.encode()
    digest = sha(data)
    backend.put_object(digest, data, media_type)
    return digest


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _published(tmp_path: Path) -> Path:
    return _doc(tmp_path) / "document.sqlite"


def _rows(tmp_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(_published(tmp_path))) as connection:
        return list(connection.execute(sql))


def test_the_three_roots_of_a_document_share_one_db_and_publish_one_file(
    tmp_path: Path,
) -> None:
    source, processing, cache = _open_all(tmp_path)
    assert isinstance(source, StagedDocumentBackend) and isinstance(
        processing, StagedDocumentBackend
    )
    assert source.kind == processing.kind == cache.kind == "staged"
    assert source.work_dir == processing.work_dir == cache.work_dir  # 一份本地工作副本
    assert source.work_dir.is_relative_to(tmp_path / "staging")
    assert staged.registered() == (_doc(tmp_path),)

    _put(source, "page svg")
    _put(processing, "envelope")
    cache.put_record(_KEY, _RECORD)
    assert not _published(tmp_path).exists()  # 运行期只写本地

    assert commit_staged(_doc(tmp_path)) == 1  # 一个文档 = 一次发布
    assert commit_staged(_doc(tmp_path)) == 0  # 没有新写入:不重发
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _files(tmp_path / "ingestion") == [f"{'d' * 64}/document.sqlite"]
    assert _rows(tmp_path, "PRAGMA journal_mode") == [("delete",)]
    assert staged.registered() == ()


def test_scopes_never_read_each_other(tmp_path: Path) -> None:
    source, processing, cache = _open_all(tmp_path)
    only_source = _put(source, "only in source")
    only_processing = _put(processing, "only in processing")
    assert processing.get_object(only_source) is None
    assert source.get_object(only_processing) is None
    assert source.object_names() == [only_source]
    assert processing.object_names() == [only_processing]
    with pytest.raises(LookupError):
        processing.content_path(only_source)  # 别的 scope 的对象 = 不存在(将写进 db)

    source.set_pointer("current-manifest", only_source)
    processing.set_pointer("current-manifest", only_processing)
    assert source.pointer("current-manifest") == only_source
    assert processing.pointer("current-manifest") == only_processing
    source.put_record("document-tree/x", b"source tree")
    assert processing.record("document-tree/x") is None

    entry = stage_envelope(b"artifact", "c" * 64)
    processing.put_stage_entry("c" * 64, entry)
    assert source.stage_entry("c" * 64) is None
    assert processing.stage_entry("c" * 64) == entry

    cache.put_record(_KEY, _RECORD)
    assert cache.record(_KEY) == _RECORD
    assert source.record(_KEY) is None and processing.record(_KEY) is None
    _close(source, processing)


def test_the_same_digest_is_stored_once_across_scopes(tmp_path: Path) -> None:
    source, processing, _ = _open_all(tmp_path)
    data = b"<svg>" + b"<g/>" * 4000 + b"</svg>"
    digest = sha(data)
    assert source.put_object(digest, data, "image/svg+xml") == "placed"
    assert commit_staged(_doc(tmp_path)) == 1
    # 新到这个 scope:字节不再写一份,但它是 processing 的新成员,要发布。
    assert processing.put_object(digest, data, "image/svg+xml") == "placed"
    assert processing.put_object(digest, data, "image/svg+xml") == "existing"
    assert processing.get_object(digest) == data and source.get_object(digest) == data
    assert commit_staged(_doc(tmp_path)) == 1
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _rows(tmp_path, "SELECT count(*) FROM objects") == [(1,)]
    assert _rows(tmp_path, "SELECT digest FROM source_object_refs") == [(digest,)]
    assert _rows(tmp_path, "SELECT digest FROM processing_object_refs") == [(digest,)]


def test_the_pdf_lives_in_the_document_db_and_reads_back_byte_identical(tmp_path: Path) -> None:
    assert STAGED_PDF_INLINE_MAX_BYTES == 64 * 1024 * 1024
    source, processing, _ = _open_all(tmp_path)
    pdf = b"%PDF-1.7\n" + os.urandom(9 * 1024 * 1024)  # 过了 8 MiB 的通用内联上限
    digest = hashlib.sha256(pdf).hexdigest()
    source.put_object(digest, pdf, "application/pdf")
    assert source.get_object(digest) == pdf
    with pytest.raises(LookupError):
        source.content_path(digest)  # 没有文件可盯:与其他内联对象同义
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _files(_doc(tmp_path)) == ["document.sqlite"]
    (external, blob) = _rows(
        tmp_path, f"SELECT external, bytes FROM objects WHERE digest = '{digest}'"
    )[0]
    assert external == 0 and isinstance(blob, bytes) and hashlib.sha256(blob).hexdigest() == digest

    shutil.rmtree(tmp_path / "staging")  # 新集群:从发布版取回
    reopened = open_backend(_doc(tmp_path) / "source", settings=_settings(tmp_path))
    assert reopened.get_object(digest) == pdf
    reopened.close()


def test_an_explicit_external_media_type_still_keeps_the_pdf_a_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path, object_store_external_media_types="application/pdf")
    source = open_backend(_doc(tmp_path) / "source", settings=settings)
    digest = _put(source, "%PDF-1.7 small", "application/pdf")
    source.close()
    release_staged(_doc(tmp_path))
    assert _files(_doc(tmp_path)) == [
        "document.sqlite",
        f"source/objects/sha256-sharded/{digest[:2]}/{digest}",
    ]


def test_a_kill_before_the_rename_keeps_the_published_file_and_the_next_commit_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, processing, cache = _open_all(tmp_path)
    first = _put(source, "first")
    assert commit_staged(_doc(tmp_path)) == 1
    before = _published(tmp_path).read_bytes()
    second = _put(processing, "second")
    cache.put_record(_KEY, _RECORD)

    def killed(_source: object, _target: object) -> None:
        raise KeyboardInterrupt  # 进程在 rename 之前被杀

    monkeypatch.setattr(os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        commit_staged(_doc(tmp_path))
    monkeypatch.undo()
    assert _published(tmp_path).read_bytes() == before

    assert commit_staged(_doc(tmp_path)) == 1  # 下一次提交补上并清掉临时名
    assert not list(_doc(tmp_path).glob(".document.sqlite.staging-*"))
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _rows(tmp_path, "SELECT digest FROM source_object_refs") == [(first,)]
    assert _rows(tmp_path, "SELECT digest FROM processing_object_refs") == [(second,)]
    assert _rows(tmp_path, "SELECT record_key FROM requests") == [(_KEY,)]


def test_a_rerun_after_local_disk_loss_restores_once_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    source, processing, cache = _open_all(tmp_path)
    digest = _put(source, "kept")
    cache.put_record(_KEY, _RECORD)
    _close(source, processing)
    release_staged(_doc(tmp_path))
    marks = _published(tmp_path).stat().st_mtime_ns
    shutil.rmtree(tmp_path / "staging")

    source, processing, cache = _open_all(tmp_path)
    assert cache.counts["restores"] == 1  # 一个文档只拷回一次
    assert source.get_object(digest) == b"kept" and cache.record(_KEY) == _RECORD
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert cache.counts["commits"] == 0
    assert _published(tmp_path).stat().st_mtime_ns == marks  # 只读回放:发布文件不动


def test_crashed_local_progress_is_published_by_the_next_run(tmp_path: Path) -> None:
    source, processing, cache = _open_all(tmp_path)
    _put(source, "published")
    commit_staged(_doc(tmp_path))
    pending = _put(processing, "pending")  # 阶段中途"崩溃":本地有,发布版没有
    cache.put_record(_KEY, _RECORD)
    document = staged._REGISTRY[_doc(tmp_path)]
    assert isinstance(document, StagedDocument)
    document.core.close()  # 模拟进程消失(不走收尾提交)
    staged._REGISTRY.clear()

    source, processing, cache = _open_all(tmp_path)
    assert cache.counts["restores"] == 0  # 发布版没被换过:保留本地进度
    assert processing.get_object(pending) == b"pending" and cache.record(_KEY) == _RECORD
    assert commit_staged(_doc(tmp_path)) == 1
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _rows(tmp_path, "SELECT digest FROM processing_object_refs") == [(pending,)]


def test_a_live_foreign_publisher_blocks_writes(tmp_path: Path) -> None:
    _doc(tmp_path).mkdir(parents=True)
    owner = live_owner(3600)
    foreign = lease.owner_payload(
        PUBLISHER_CLAIM_FORMAT,
        type(owner)(owner.host, owner.pid, "foreign-token", owner.created_at, 3600),
    )
    lease.write_lease(_doc(tmp_path) / "document.sqlite.publisher", foreign)
    source = open_backend(_doc(tmp_path) / "source", settings=_settings(tmp_path))
    with pytest.raises(StoreBusy) as caught:
        _put(source, "blocked")
    assert str(caught.value) == "store_busy"
    source.close()


def test_concurrent_writers_and_nested_transactions_across_scopes(tmp_path: Path) -> None:
    """页级并发(ADR 0045)的几个线程同时写三个 scope;一个 scope 的事务里写另一个 scope
    (同一个连接、同一个事务,不会自己等自己的写锁)。"""
    source, processing, cache = _open_all(tmp_path)
    with processing.transaction():
        inner = _put(processing, "outer")
        nested = _put(source, "nested in a processing transaction")
        cache.put_record("e" * 64, b"{}")
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            for step in range(20):
                _put(source, f"s-{index}-{step}")
                _put(processing, f"p-{index}-{step}")
                cache.put_record(f"{index:02d}{step:02d}" + "f" * 60, b"{}")
        except Exception as error:  # noqa: BLE001  # pragma: no cover - 失败时报出来
            errors.append(error)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(source.object_names()) == 81 and len(processing.object_names()) == 81
    assert processing.get_object(inner) == b"outer" and source.get_object(nested) is not None
    _close(source, processing)
    release_staged(_doc(tmp_path))
    assert _rows(tmp_path, "SELECT count(*) FROM requests") == [(81,)]


@pytest.mark.parametrize(
    "legacy",
    [
        "source/store.sqlite",
        "processing/store.sqlite",
        "processing/model-cache/model-cache.sqlite",
    ],
)
def test_the_four_file_layout_is_a_layout_mismatch(tmp_path: Path, legacy: str) -> None:
    path = _doc(tmp_path) / legacy
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    with pytest.raises(LayoutMismatch) as raised:
        open_backend(_doc(tmp_path) / "source", settings=_settings(tmp_path))
    assert "layout_mismatch" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)  # 隐私:不带路径
    assert staged.registered() == ()


@pytest.mark.parametrize("mode", ["files", "sqlite", "auto"])
def test_other_modes_refuse_a_document_db(tmp_path: Path, mode: str) -> None:
    source, processing, _ = _open_all(tmp_path)
    _put(source, "staged")
    _close(source, processing)
    release_staged(_doc(tmp_path))
    settings = Settings(object_store_backend=mode)  # type: ignore[arg-type]
    for root, kind in (
        (_doc(tmp_path) / "source", "object"),
        (_doc(tmp_path) / "processing", "object"),
        (_doc(tmp_path) / "processing" / "model-cache", "model-cache"),
    ):
        with pytest.raises(LayoutMismatch, match="layout_mismatch"):
            open_backend(root, kind, settings=settings)  # type: ignore[call-overload]


def test_default_modes_keep_one_store_db_per_store_root(tmp_path: Path) -> None:
    source = open_backend(
        _doc(tmp_path) / "source", settings=Settings(object_store_backend="sqlite")
    )
    assert isinstance(source, SqliteBackend) and not isinstance(source, StagedDocumentBackend)
    _put(source, "plain")
    source.close()
    files = open_backend(
        _doc(tmp_path) / "processing", settings=Settings(object_store_backend="files")
    )
    assert isinstance(files, FileBackend)
    _put(files, "plain")
    assert not _published(tmp_path).exists()
    assert "source/store.sqlite" in _files(_doc(tmp_path))
    assert staged.registered() == ()
