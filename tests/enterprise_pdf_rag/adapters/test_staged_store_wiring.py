"""ADR 0040:``APP_OBJECT_STORE_BACKEND=staged`` 下的 run-folder 端到端。

- 逻辑字节与发布 id 与 files / sqlite 相同(``FULL_STORE_DIGEST`` / ``FULL_PUBLISHED_ID``);
- 发布目录里每个 store 根只有一个整文件 ``store.sqlite``(没有 -wal / 写者租约残留),
  本地工作副本在 ``APP_OBJECT_STORE_STAGING_DIR`` 下;
- 每个阶段边界都提交了该文档的 store,文档结束后注册表里不再有它;
- 本地盘被清空后重跑:从发布版拷回,零模型调用,同一发布 id。
"""

import shutil
from collections.abc import Iterator
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
    real_commit = folder_pipeline.commit_staged

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

    document_root = (tmp_path / "ingestion").resolve() / document.sha256
    for store in ("source", "processing"):
        assert (document_root / store / "store.sqlite").is_file()
        leftovers = [
            path.name
            for path in (document_root / store).iterdir()
            if path.name.startswith(("store.sqlite-", "store.sqlite.", ".store.sqlite"))
        ]
        assert leftovers == []
    assert any(staged_backend.rglob("store.sqlite"))  # 本地工作副本
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
    assert any(staged_backend.rglob("store.sqlite"))
