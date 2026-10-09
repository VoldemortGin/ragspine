"""StagedBackend(enterprise-pdf-rag ADR 0044):本地盘 sqlite 工作副本 + 阶段结束整文件发布。

- commit 后发布目录可见,且是整文件原子替换(临时名 → rename);
- 中途被杀(临时文件已写、尚未 rename):发布版原样不动,下一次 commit 清掉残留;
- 断点续跑:本地没有工作副本 → 从发布版整文件拷回;发布版没被别处换过 → 保留本地进度;
- 注册表:同一 store 根在进程内共享一个实例,store 的 close 不收尾,
  ``commit_staged`` / ``release_staged`` 按目录前缀提交 / 收尾;
- 默认配置不走 staged;staged 模式下模型缓存仍是文件布局;
- 发布目录的发布者租约:另一个活着的进程持有时写入即 ``StoreBusy``。
"""

import sqlite3
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
from ragspine.common.evidence.object_backend.staged import (
    PUBLISHER_CLAIM_FORMAT,
    StagedBackend,
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

    monkeypatch.setattr(staged.os, "replace", killed)
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
    return Settings(  # type: ignore[call-arg]
        object_store_backend="staged", object_store_staging_dir=tmp_path / "staging"
    )


def test_open_backend_shares_one_instance_per_store_root(tmp_path: Path) -> None:
    settings = _staged_settings(tmp_path)
    document = tmp_path / "ingestion" / "doc-a"
    first = open_backend(document / "source", settings=settings)
    second = open_backend(document / "source", settings=settings)
    other = open_backend(document / "processing", settings=settings)
    assert isinstance(first, StagedBackend) and first is second and other is not first
    assert first.kind == "staged"
    assert first.work_dir.is_relative_to(tmp_path / "staging")
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
    assert Settings().object_store_backend == "auto"  # type: ignore[call-arg]
    assert Settings().object_store_staging_dir is None  # type: ignore[call-arg]
    files = open_backend(tmp_path / "s", settings=Settings(object_store_backend="files"))  # type: ignore[call-arg]
    assert isinstance(files, FileBackend)
    auto = open_backend(tmp_path / "t", settings=Settings())  # type: ignore[call-arg]
    assert not isinstance(auto, StagedBackend)
    auto.close()
    assert staged.registered() == ()
