"""ADR 0044:``APP_OBJECT_STORE_BACKEND=staged`` 下的 run-folder 端到端。

- 逻辑字节与发布 id 与 files / sqlite 相同(``FULL_STORE_DIGEST`` / ``FULL_PUBLISHED_ID``);
- 发布目录里每个 store 根只有一个整文件 ``store.sqlite``(没有 -wal / 写者租约残留),
  本地工作副本在 ``APP_OBJECT_STORE_STAGING_DIR`` 下;
- 每个阶段边界都提交了该文档的 store,文档结束后注册表里不再有它;
- 本地盘被清空后重跑:从发布版拷回,零模型调用,同一发布 id;
- ADR 0046:文档自己的模型缓存也 staged;在某次发布的 rename 之前被杀后重跑能恢复。
- ADR 0047:每份文档一个 db——文档结束后发布目录只有 **1 个**整文件 ``document.sqlite``
  (两个 store、模型缓存与 PDF 原件都在里面),没有 -wal / -shm / 租约 / 临时名。
"""

import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters import folder_pipeline
from ragspine.common.evidence.configs import get_settings
from ragspine.common.evidence.object_backend import staged
from ragspine.common.evidence.object_backend.probe import clear_probe_cache
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    FULL_STORE_DIGEST,
    lite_env,
    mixed_folder,
    run_mode,
    store_digest,
)


@pytest.fixture
def staged_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    staging = tmp_path / "staging"
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", "staged")
    monkeypatch.setenv("APP_OBJECT_STORE_STAGING_DIR", str(staging))
    # ADR 0048: staged 默认不落盘派生产物;这里钉死的是 staged 机制本身的逐字节等价,
    # 所以显式落盘。staged 的默认值见 test_derived_artifacts。
    monkeypatch.setenv("APP_PERSIST_DERIVED_ARTIFACTS", "true")
    get_settings.cache_clear()
    clear_probe_cache()
    yield staging
    staged.release_staged(tmp_path)
    get_settings.cache_clear()


def test_staged_run_publishes_whole_store_files_with_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staged_backend: Path
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    boundaries: list[Path] = []
    real_commit = staged.commit_staged

    def spy(prefix: Path) -> int:
        boundaries.append(prefix)
        return real_commit(prefix)

    monkeypatch.setattr(folder_pipeline, "commit_staged", spy)
    (document,) = run_mode(tmp_path, "full").documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert document.object_backend == "staged"
    digest, _, requests = store_digest(tmp_path / "ingestion")
    assert (digest, requests) == (FULL_STORE_DIGEST, FULL_REQUESTS_DIGEST)

    assert document.sha256 is not None
    document_root = (tmp_path / "ingestion").resolve() / document.sha256
    assert (document_root / "document.sqlite").is_file()
    assert not any(document_root.rglob("store.sqlite")) and not any(
        document_root.rglob("model-cache.sqlite")
    )
    leftovers = [
        path.name
        for path in document_root.iterdir()
        if path.name.startswith(("document.sqlite-", "document.sqlite.", ".document.sqlite"))
    ]
    assert leftovers == []
    assert any(staged_backend.rglob("document.sqlite"))  # 本地工作副本
    # requalify / qualify / index / publish 四个阶段边界 + 文档结束各提交一次。
    assert boundaries == [document_root] * 5
    assert not [root for root in staged.registered() if root.is_relative_to(document_root)]


def test_staged_rerun_after_local_disk_loss_restores_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staged_backend: Path
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    run_mode(tmp_path, "full")
    shutil.rmtree(staged_backend)  # 新集群:本地盘是空的
    tasks.clear()

    (document,) = run_mode(tmp_path, "full").documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert sum(tasks.values()) == 0
    assert any(staged_backend.rglob("document.sqlite"))


def _document_files(document_root: Path) -> list[str]:
    return sorted(
        path.relative_to(document_root).as_posix()
        for path in document_root.rglob("*")
        if path.is_file()
    )


def test_staged_run_leaves_one_whole_file_per_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staged_backend: Path
) -> None:
    lite_env(monkeypatch)
    mixed_folder(tmp_path)
    # lite:不写审阅页(full 的 runs/<id>/ 审阅页按需用 export_document_review 生成)。
    (document,) = run_mode(tmp_path, "lite").documents
    assert document.status == "published" and document.sha256 is not None
    document_root = (tmp_path / "ingestion").resolve() / document.sha256

    assert _document_files(document_root) == ["document.sqlite"]
    published = document_root / "document.sqlite"
    with closing(sqlite3.connect(published)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        # PDF 原件在库里(内联行),字节与源文件一致。
        (pdf,) = connection.execute(
            "SELECT bytes FROM source_objects WHERE media_type = 'application/pdf'"
        ).fetchone()
    assert bytes(pdf) == (tmp_path / "pdfs" / "mixed.pdf").read_bytes()
    assert staged.registered() == ()


def test_staged_rerun_after_a_kill_before_a_publish_rename_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staged_backend: Path
) -> None:
    tasks = lite_env(monkeypatch)
    mixed_folder(tmp_path)
    real_replace = os.replace
    published: list[str] = []

    def killed_on_third_publish(source: str | Path, target: str | Path) -> None:
        if ".staging-" in Path(source).name:
            published.append(Path(target).name)
            if len(published) >= 3:
                raise KeyboardInterrupt  # 进程在这次发布的 rename 之前被杀:之后什么都发不出去
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", killed_on_third_publish)
    with pytest.raises(KeyboardInterrupt):
        run_mode(tmp_path, "full")
    monkeypatch.setattr(os, "replace", real_replace)
    staged._REGISTRY.clear()  # 进程没了:注册表与连接都不在了(本地副本还在)
    first_calls = sum(tasks.values())
    tasks.clear()

    (document,) = run_mode(tmp_path, "full").documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert len(published) > 3  # 收尾的提交也都没发出去
    assert first_calls > 0 and sum(tasks.values()) == 0  # 本地副本里的模型结果全部回放
    digest, _, requests = store_digest(tmp_path / "ingestion")
    assert (digest, requests) == (FULL_STORE_DIGEST, FULL_REQUESTS_DIGEST)
    assert document.sha256 is not None
    document_root = (tmp_path / "ingestion").resolve() / document.sha256
    leftovers = [
        name
        for name in _document_files(document_root)
        if re.search(r"(-wal|-shm|\.writer|\.publisher|\.staging-)", name)
    ]
    assert leftovers == []
