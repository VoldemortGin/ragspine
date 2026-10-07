"""探测的各失败路径(含 Volumes 式随机写不可用与 -shm 不可用)与注册表的三种模式。"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import Settings
from ragspine.common.evidence.object_backend.files import FileBackend, FileModelCacheBackend
from ragspine.common.evidence.object_backend.probe import (
    ProbeResult,
    clear_probe_cache,
    probe_directory,
)
from ragspine.common.evidence.object_backend.protocol import BackendUnavailable
from ragspine.common.evidence.object_backend.registry import external_media_types, open_backend
from ragspine.common.evidence.object_backend.sqlite import SqliteBackend, SqliteModelCacheBackend


@pytest.fixture(autouse=True)
def fresh_probe_cache() -> None:
    clear_probe_cache()


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# ---- 探测 ---------------------------------------------------------------------------------


def test_probe_succeeds_on_a_real_directory_and_cleans_up(tmp_path: Path) -> None:
    result = probe_directory(tmp_path)
    assert result == ProbeResult(True, None, "NORMAL")
    assert list(tmp_path.iterdir()) == []  # 探测的临时 db / -wal / -shm 全部清掉


def test_probe_result_is_cached_per_directory(tmp_path: Path) -> None:
    assert probe_directory(tmp_path).ok

    def refuse(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    assert probe_directory(tmp_path, connect=refuse).ok  # 缓存命中,根本不再连接
    assert not probe_directory(tmp_path, connect=refuse, refresh=True).ok


def test_probe_random_write_failure_like_volumes(tmp_path: Path) -> None:
    """Volumes 式不支持随机写:COMMIT 时 disk I/O error → sqlite_write,不含路径。"""

    class CommitRefusingConnection:
        def __init__(self, path: str) -> None:
            self._real = sqlite3.connect(path, isolation_level=None)

        def execute(self, sql: str, parameters: Sequence[object] = (), /) -> object:
            if sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, parameters)

        def close(self) -> None:
            self._real.close()

    result = probe_directory(
        tmp_path,
        refresh=True,
        connect=lambda path: CommitRefusingConnection(path),  # type: ignore[arg-type,return-value]
    )
    assert result == ProbeResult(False, "sqlite_write", "NORMAL")
    assert str(tmp_path) not in (result.code or "")


def test_probe_connect_failure(tmp_path: Path) -> None:
    def refuse(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    assert probe_directory(tmp_path, refresh=True, connect=refuse) == ProbeResult(
        False, "sqlite_connect", "NORMAL"
    )


def test_probe_shm_unavailable_falls_back_to_exclusive(tmp_path: Path) -> None:
    """第二个连接读不回(-shm 不可用)→ EXCLUSIVE 候选可用 → ok=EXCLUSIVE。"""
    calls = {"count": 0}

    def connect(path: str) -> sqlite3.Connection:
        calls["count"] += 1
        if calls["count"] == 2:  # 写者之后的读者连接
            raise sqlite3.OperationalError("shm unavailable")
        return sqlite3.connect(path, isolation_level=None)

    result = probe_directory(tmp_path, refresh=True, connect=connect)
    assert result == ProbeResult(True, None, "EXCLUSIVE")


def test_probe_shm_and_exclusive_unavailable(tmp_path: Path) -> None:
    calls = {"count": 0}

    def connect(path: str) -> sqlite3.Connection:
        calls["count"] += 1
        if calls["count"] >= 2:  # 读者连接与 EXCLUSIVE 候选都失败
            raise sqlite3.OperationalError("shm unavailable")
        return sqlite3.connect(path, isolation_level=None)

    assert probe_directory(tmp_path, refresh=True, connect=connect) == ProbeResult(
        False, "sqlite_shm", "NORMAL"
    )


# ---- 注册表 -------------------------------------------------------------------------------


def test_files_mode_returns_the_file_layout_without_probing(tmp_path: Path) -> None:
    settings = _settings(object_store_backend="files")
    backend = open_backend(tmp_path / "does-not-exist", settings=settings)
    assert isinstance(backend, FileBackend)
    cache = open_backend(tmp_path / "cache", "model-cache", settings=settings)
    assert isinstance(cache, FileModelCacheBackend)
    assert not (tmp_path / "does-not-exist").exists()  # files 模式不探测、不建目录


def test_auto_mode_uses_sqlite_where_the_probe_passes(tmp_path: Path) -> None:
    settings = _settings(object_store_backend="auto")
    backend = open_backend(tmp_path / "store", settings=settings)
    assert isinstance(backend, SqliteBackend)
    backend.close()
    cache = open_backend(tmp_path / "cache", "model-cache", settings=settings)
    assert isinstance(cache, SqliteModelCacheBackend)
    cache.close()


def test_auto_mode_falls_back_to_files_when_the_probe_fails(tmp_path: Path) -> None:
    def refuse(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    probe_directory(tmp_path / "store", refresh=True, connect=refuse)  # 毒化该目录的缓存
    backend = open_backend(tmp_path / "store", settings=_settings(object_store_backend="auto"))
    assert isinstance(backend, FileBackend)


def test_explicit_sqlite_mode_refuses_when_the_probe_fails(tmp_path: Path) -> None:
    def refuse(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    probe_directory(tmp_path / "store", refresh=True, connect=refuse)
    with pytest.raises(BackendUnavailable) as caught:
        open_backend(tmp_path / "store", settings=_settings(object_store_backend="sqlite"))
    message = str(caught.value)
    assert "APP_OBJECT_STORE_BACKEND" in message and "files" in message  # 告诉用户怎么改回
    assert "sqlite_connect" in message  # 带失败码
    assert str(tmp_path) not in message  # 不带路径


def test_external_media_types_parsing() -> None:
    settings = _settings(object_store_external_media_types="application/pdf, image/svg+xml, ,")
    assert external_media_types(settings) == frozenset({"application/pdf", "image/svg+xml"})


def test_settings_defaults() -> None:
    settings = _settings()
    assert settings.object_store_backend == "auto"
    assert settings.object_store_inline_max_bytes == 262_144
    assert settings.object_store_external_media_types == "application/pdf"
    assert settings.object_store_synchronous == "FULL"
    assert settings.object_store_max_db_bytes == 400 * 1024 * 1024
