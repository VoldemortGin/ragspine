"""StagedBackend(enterprise-pdf-rag ADR 0044):本地盘 sqlite 工作副本 + 阶段结束整文件发布。

- commit 后发布目录可见,且是整文件原子替换(临时名 → rename);
- 中途被杀(临时文件已写、尚未 rename):发布版原样不动,下一次 commit 清掉残留;
- 断点续跑:本地没有工作副本 → 从发布版整文件拷回;发布版没被别处换过 → 保留本地进度;
- 注册表:同一 store 根在进程内共享一个实例,store 的 close 不收尾,
  ``commit_staged`` / ``release_staged`` 按目录前缀提交 / 收尾;
- 默认配置不走 staged;staged 模式下根级模型缓存仍是文件布局;
- 发布目录的发布者租约:另一个活着的进程持有时写入即 ``StoreBusy``;
- ADR 0046:文档自己的模型缓存(``processing/model-cache``)随它的 store 一起 staged、
  整文件发布、可续跑;staged 默认内联到 8 MiB(zlib,digest 仍是原始字节的);
  文档结束后发布目录只剩整文件,没有 -wal / -shm / 租约。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import Settings
from ragspine.common.evidence.object_backend import lease, staged
from ragspine.common.evidence.object_backend.files import FileBackend, FileModelCacheBackend
from ragspine.common.evidence.object_backend.probe import clear_probe_cache
from ragspine.common.evidence.object_backend.protocol import ObjectBackend, StoreBusy
from ragspine.common.evidence.object_backend.registry import open_backend
from ragspine.common.evidence.object_backend.sqlite import DEFAULT_INLINE_MAX_BYTES
from ragspine.common.evidence.object_backend.staged import (
    PUBLISHER_CLAIM_FORMAT,
    STAGED_INLINE_MAX_BYTES,
    StagedBackend,
    StagedModelCacheBackend,
    commit_staged,
    release_staged,
)
from tests.enterprise_pdf_rag.object_backend.conftest import live_owner, sha


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_probe_cache()
    yield
    release_staged(Path("/"))


def _put(backend: ObjectBackend, text: str) -> str:
    data = text.encode()
    digest = sha(data)
    backend.put_object(digest, data, "text/plain")
    return digest


def _published_digests(publish: Path) -> set[str]:
    with closing(sqlite3.connect(publish / "store.sqlite")) as connection:
        return {row[0] for row in connection.execute("SELECT digest FROM objects")}


def _staging_leftovers(publish: Path) -> list[Path]:
    return sorted(publish.glob(".store.sqlite.staging-*"))


def test_commit_publishes_one_whole_file_and_skips_when_clean(tmp_path: Path) -> None:
    publish, work = tmp_path / "publish", tmp_path / "work"
    backend = StagedBackend(publish, work_dir=work)
    first = _put(backend, "page one")
    assert not (publish / "store.sqlite").exists()  # 运行期只写本地
    assert (work / "store.sqlite").is_file()

    assert backend.commit() is True
    assert _published_digests(publish) == {first}
    assert not (publish / "store.sqlite-wal").exists()  # 发布的是自足的整文件
    assert _staging_leftovers(publish) == []
    assert backend.commit() is False  # 没有新写入:不重发

    second = _put(backend, "page two")
    assert backend.commit() is True
    assert _published_digests(publish) == {first, second}
    assert backend.counts["commits"] == 2 and backend.counts["published_bytes"] > 0
    backend.close()


def test_killed_before_rename_leaves_the_published_version_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish, work = tmp_path / "publish", tmp_path / "work"
    backend = StagedBackend(publish, work_dir=work)
    first = _put(backend, "committed")
    backend.commit()
    before = (publish / "store.sqlite").read_bytes()
    _put(backend, "not yet published")

    def killed(_source: object, _target: object) -> None:
        raise KeyboardInterrupt  # 进程在 rename 之前被杀

    monkeypatch.setattr(os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        backend.commit()
    monkeypatch.undo()
    assert (publish / "store.sqlite").read_bytes() == before

    # 真正的 kill 不会跑清理:留下一个写了一半的临时文件。它不影响读者 / 续跑。
    (publish / ".store.sqlite.staging-999-dead").write_bytes(b"half a database")
    reader = StagedBackend(publish, work_dir=tmp_path / "elsewhere")
    assert reader.get_object(first) == b"committed"
    reader.close()

    assert backend.commit() is True  # 未发布的写入仍记着,下一次提交补上并清残留
    assert len(_published_digests(publish)) == 2
    assert _staging_leftovers(publish) == []
    backend.close()


def test_resume_copies_the_published_version_back_when_local_is_gone(tmp_path: Path) -> None:
    publish = tmp_path / "publish"
    backend = StagedBackend(publish, work_dir=tmp_path / "work-a")
    digest = _put(backend, "evidence")
    backend.set_pointer("current-processing", digest)
    backend.put_record("document-tree/x.json", b"{}")
    backend.close()  # 最后一个引用:提交并收尾

    resumed = StagedBackend(publish, work_dir=tmp_path / "work-b")  # 新机器 / 本地盘已清
    assert resumed.counts["restores"] == 1
    assert resumed.pointer("current-processing") == digest
    assert resumed.record("document-tree/x.json") == b"{}"
    assert resumed.get_object(digest) == b"evidence"
    resumed.close()


def test_local_progress_is_kept_while_the_published_version_is_unchanged(tmp_path: Path) -> None:
    publish, work = tmp_path / "publish", tmp_path / "work"
    crashed = StagedBackend(publish, work_dir=work)
    committed = _put(crashed, "committed")
    crashed.commit()
    pending = _put(crashed, "pending")  # 阶段中途"崩溃":本地有,发布版没有
    crashed._core.close()  # 模拟进程消失(不走 close 的收尾提交)

    resumed = StagedBackend(publish, work_dir=work)
    assert resumed.counts["restores"] == 0
    assert resumed.get_object(pending) == b"pending"
    assert resumed.get_object(committed) == b"committed"
    resumed.close()  # 没有新写入也要补发:本地内容与上次发布的不同
    assert _published_digests(publish) == {committed, pending}


def test_a_published_version_replaced_elsewhere_wins_over_a_stale_local_copy(
    tmp_path: Path,
) -> None:
    publish = tmp_path / "publish"
    stale = StagedBackend(publish, work_dir=tmp_path / "work")
    _put(stale, "old")
    stale.close()
    other = StagedBackend(publish, work_dir=tmp_path / "other-machine")
    newer = _put(other, "newer")
    other.close()

    again = StagedBackend(publish, work_dir=tmp_path / "work")
    assert again.counts["restores"] == 1
    assert again.get_object(newer) == b"newer"
    again.close()


def test_a_live_foreign_publisher_blocks_writes(tmp_path: Path) -> None:
    publish = tmp_path / "publish"
    publish.mkdir()
    owner = live_owner(3600)
    foreign = lease.owner_payload(
        PUBLISHER_CLAIM_FORMAT,
        type(owner)(owner.host, owner.pid, "foreign-token", owner.created_at, 3600),
    )
    lease.write_lease(publish / "store.sqlite.publisher", foreign)
    backend = StagedBackend(publish, work_dir=tmp_path / "work")
    with pytest.raises(StoreBusy) as caught:
        _put(backend, "x")
    assert str(caught.value) == "store_busy"
    backend.close()


def test_the_publisher_lease_is_released_on_close(tmp_path: Path) -> None:
    publish = tmp_path / "publish"
    backend = StagedBackend(publish, work_dir=tmp_path / "work")
    _put(backend, "x")
    assert (publish / "store.sqlite.publisher").is_file()
    backend.close()
    assert not (publish / "store.sqlite.publisher").exists()


# ---- 注册表与选择 ----------------------------------------------------------------------


def _staged_settings(tmp_path: Path) -> Settings:
    return Settings(object_store_backend="staged", object_store_staging_dir=tmp_path / "staging")


def test_open_backend_shares_one_instance_per_store_root(tmp_path: Path) -> None:
    settings = _staged_settings(tmp_path)
    document = tmp_path / "ingestion" / "doc-a"
    first = open_backend(document / "source", settings=settings)
    second = open_backend(document / "source", settings=settings)
    other = open_backend(document / "processing", settings=settings)
    assert isinstance(first, StagedBackend) and first is second and other is not first
    assert first.kind == "staged"
    assert first.work_dir.is_relative_to(tmp_path / "staging")
    assert isinstance(other, StagedBackend)
    assert other.work_dir != first.work_dir  # 每个 store 根各自一份本地副本

    digest = _put(first, "stage one")
    first.close()  # store 的 close:注册表仍持有,不收尾
    second.close()
    assert not (document / "source" / "store.sqlite").exists()

    assert commit_staged(document) == 1  # 阶段边界:只有写过的那个被提交
    assert _published_digests(document / "source") == {digest}
    third = open_backend(document / "source", settings=settings)
    assert third is first  # 阶段间同一实例
    third.close()

    _put(first, "stage two")
    release_staged(document)  # 文档结束:提交并收尾,离开注册表
    assert len(_published_digests(document / "source")) == 2
    assert open_backend(document / "source", settings=settings) is not first


def test_documents_never_share_a_local_copy(tmp_path: Path) -> None:
    settings = _staged_settings(tmp_path)
    a = open_backend(tmp_path / "ingestion" / "a" / "source", settings=settings)
    b = open_backend(tmp_path / "ingestion" / "b" / "source", settings=settings)
    _put(a, "only in a")
    assert commit_staged(tmp_path / "ingestion" / "b") == 0
    assert isinstance(a, StagedBackend) and isinstance(b, StagedBackend)
    assert a.work_dir != b.work_dir
    a.close()
    b.close()


def test_staged_mode_keeps_the_model_cache_on_the_file_layout(tmp_path: Path) -> None:
    cache = open_backend(
        tmp_path / "model-cache", "model-cache", settings=_staged_settings(tmp_path)
    )
    assert isinstance(cache, FileModelCacheBackend)


def test_default_settings_never_choose_the_staged_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_OBJECT_STORE_BACKEND", raising=False)  # 测试进程钉的是 files
    monkeypatch.delenv("APP_OBJECT_STORE_STAGING_DIR", raising=False)
    assert Settings.model_fields["object_store_backend"].default == "auto"
    assert Settings().object_store_backend == "auto"
    assert Settings().object_store_staging_dir is None
    files = open_backend(tmp_path / "s", settings=Settings(object_store_backend="files"))
    assert isinstance(files, FileBackend)
    auto = open_backend(tmp_path / "t", settings=Settings())
    assert not isinstance(auto, StagedBackend)
    auto.close()
    assert staged.registered() == ()


# ---- ADR 0046:每份文档 4 个文件 ---------------------------------------------------------

_KEY = "a" * 64
_RECORD = b'{"request_fingerprint": "' + b"a" * 64 + b'", "response_digest": null}'


def _document(tmp_path: Path) -> Path:
    return tmp_path / "ingestion" / "doc-a"


def _open_document_cache(tmp_path: Path) -> tuple[ObjectBackend, StagedModelCacheBackend]:
    settings = _staged_settings(tmp_path)
    processing = _document(tmp_path) / "processing"
    store = open_backend(processing, settings=settings)
    cache = open_backend(processing / "model-cache", "model-cache", settings=settings)
    assert isinstance(cache, StagedModelCacheBackend)
    return store, cache


def _published_cache(tmp_path: Path) -> Path:
    return _document(tmp_path) / "processing" / "model-cache" / "model-cache.sqlite"


def _published_records(path: Path) -> dict[str, bytes]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(key): bytes(record)
            for key, record in connection.execute("SELECT record_key, record FROM requests")
        }


def _files_under(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_a_document_model_cache_is_staged_with_its_store_and_published_whole(
    tmp_path: Path,
) -> None:
    store, cache = _open_document_cache(tmp_path)
    assert cache.kind == "staged"
    assert cache.work_dir.is_relative_to(tmp_path / "staging")
    again = open_backend(
        _document(tmp_path) / "processing" / "model-cache",
        "model-cache",
        settings=_staged_settings(tmp_path),
    )
    assert again is cache  # 同一文档的各个 client 共享一个实例
    cache.put_record(_KEY, _RECORD)
    assert not _published_cache(tmp_path).exists()  # 运行期只写本地

    assert commit_staged(_document(tmp_path)) == 1  # store 没写过:只发布模型缓存
    published = _published_cache(tmp_path)
    assert _published_records(published) == {_KEY: _RECORD}
    with closing(sqlite3.connect(published)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert commit_staged(_document(tmp_path)) == 0  # 没有新写入:不重发
    assert cache.counts["commits"] == 1

    store.close()
    release_staged(_document(tmp_path))
    # 文档结束:发布目录只剩整文件,没有 -wal / -shm / .writer / .publisher / 临时名。
    assert _files_under(tmp_path / "ingestion") == [
        "doc-a/processing/model-cache/model-cache.sqlite"
    ]
    assert staged.registered() == ()


def test_a_staged_model_cache_resumes_from_the_published_file(tmp_path: Path) -> None:
    store, cache = _open_document_cache(tmp_path)
    cache.put_record(_KEY, _RECORD)
    store.close()
    release_staged(_document(tmp_path))
    shutil.rmtree(tmp_path / "staging")  # 新集群:本地盘是空的

    store, cache = _open_document_cache(tmp_path)
    assert cache.counts["restores"] == 1
    assert cache.record(_KEY) == _RECORD
    store.close()
    release_staged(_document(tmp_path))
    assert cache.counts["commits"] == 0  # 只读回放:不重发


def test_a_killed_model_cache_publish_keeps_the_published_version_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, cache = _open_document_cache(tmp_path)
    cache.put_record(_KEY, _RECORD)
    commit_staged(_document(tmp_path))
    before = _published_cache(tmp_path).read_bytes()
    cache.put_record("b" * 64, b'{"second": true}')

    def killed(_source: object, _target: object) -> None:
        raise KeyboardInterrupt  # 进程在 rename 之前被杀

    monkeypatch.setattr(os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        commit_staged(_document(tmp_path))
    monkeypatch.undo()
    assert _published_cache(tmp_path).read_bytes() == before

    assert commit_staged(_document(tmp_path)) == 1  # 下一次提交补上
    assert set(_published_records(_published_cache(tmp_path))) == {_KEY, "b" * 64}
    assert not list(_published_cache(tmp_path).parent.glob(".model-cache.sqlite.staging-*"))
    store.close()
    release_staged(_document(tmp_path))


def test_crashed_model_cache_progress_is_published_by_the_next_run(tmp_path: Path) -> None:
    store, cache = _open_document_cache(tmp_path)
    cache.put_record(_KEY, _RECORD)
    commit_staged(_document(tmp_path))
    cache.put_record("b" * 64, b'{"pending": true}')  # 阶段中途"崩溃":本地有,发布版没有
    cache._core.close()  # 模拟进程消失(不走收尾提交)
    staged._REGISTRY.clear()
    store.close()

    store, resumed = _open_document_cache(tmp_path)
    assert resumed is not cache and resumed.counts["restores"] == 0
    assert resumed.record("b" * 64) == b'{"pending": true}'
    assert commit_staged(_document(tmp_path)) == 1
    assert set(_published_records(_published_cache(tmp_path))) == {_KEY, "b" * 64}
    store.close()
    release_staged(_document(tmp_path))


def test_a_model_cache_outside_any_staged_store_stays_on_the_file_layout(
    tmp_path: Path,
) -> None:
    settings = _staged_settings(tmp_path)
    open_backend(_document(tmp_path) / "processing", settings=settings).close()
    root_cache = open_backend(
        tmp_path / "ingestion" / "model-cache", "model-cache", settings=settings
    )
    assert isinstance(root_cache, FileModelCacheBackend)  # 根级答案缓存:多进程写,不 staged


def _svg(size: int) -> bytes:
    body = b"".join(b'<path d="M%d 0 L0 %d"/>' % (index, index) for index in range(size // 24))
    return b'<svg xmlns="http://www.w3.org/2000/svg">' + body + b"</svg>"


def test_staged_mode_inlines_objects_up_to_8_mib_compressed_by_default(tmp_path: Path) -> None:
    assert STAGED_INLINE_MAX_BYTES == 8 * 1024 * 1024
    data = _svg(2_400_000)  # 与 71 页样本最大的页 SVG 同量级
    digest = hashlib.sha256(data).hexdigest()
    root = _document(tmp_path) / "source"
    backend = open_backend(root, settings=_staged_settings(tmp_path))
    assert isinstance(backend, StagedBackend)
    backend.put_object(digest, data, "image/svg+xml")
    assert backend.get_object(digest) == data
    backend.close()
    release_staged(_document(tmp_path))
    assert _files_under(root) == ["store.sqlite"]  # 没有外置文件
    with closing(sqlite3.connect(root / "store.sqlite")) as connection:
        length, encoding, external, blob = connection.execute(
            "SELECT byte_length, encoding, external, bytes FROM objects WHERE digest = ?",
            (digest,),
        ).fetchone()
    assert (length, encoding, external) == (len(data), "zlib", 0)
    assert len(blob) < len(data) // 4
    assert hashlib.sha256(zlib.decompress(blob)).hexdigest() == digest  # digest 按原始字节


def test_an_explicit_inline_limit_still_wins_in_staged_mode(tmp_path: Path) -> None:
    data = _svg(400_000)
    digest = hashlib.sha256(data).hexdigest()
    settings = Settings(
        object_store_backend="staged",
        object_store_staging_dir=tmp_path / "staging",
        object_store_inline_max_bytes=DEFAULT_INLINE_MAX_BYTES,
    )
    root = _document(tmp_path) / "source"
    backend = open_backend(root, settings=settings)
    backend.put_object(digest, data, "image/svg+xml")
    backend.close()
    release_staged(_document(tmp_path))
    assert (root / "objects" / "sha256-sharded" / digest[:2] / digest).read_bytes() == data


def test_the_sqlite_backend_keeps_the_256_kib_default(tmp_path: Path) -> None:
    data = _svg(400_000)
    digest = hashlib.sha256(data).hexdigest()
    backend = open_backend(tmp_path / "s", settings=Settings(object_store_backend="sqlite"))
    backend.put_object(digest, data, "image/svg+xml")
    backend.close()
    assert (tmp_path / "s" / "objects" / "sha256-sharded" / digest[:2] / digest).is_file()


def test_the_model_cache_signature_ignores_claims(tmp_path: Path) -> None:
    """claim 只是运行期互斥:只有 claim 进出的模型缓存不必重发。"""
    store, cache = _open_document_cache(tmp_path)
    cache.put_record(_KEY, _RECORD)
    assert commit_staged(_document(tmp_path)) == 1
    owner = live_owner()
    assert cache.claim("c" * 64, owner) == 0
    cache.release("c" * 64, owner)
    assert commit_staged(_document(tmp_path)) == 0
    store.close()
    release_staged(_document(tmp_path))
    assert (
        json.loads(_published_records(_published_cache(tmp_path))[_KEY])["response_digest"] is None
    )
