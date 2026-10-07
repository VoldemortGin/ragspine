"""sqlite 可用性探测(设计稿 §6):在目标目录里真实地建库、写、读回、体检、清理。

目标文件系统(Databricks Workspace files / Volumes 一类 FUSE)可能不支持随机写、
``-shm`` 共享内存或可靠的文件锁。探测按序验证每一步;任一步失败 → ``ok=False``,
``code`` 只含失败步骤(``sqlite_<step>``),**不含路径**,也不含底层错误文本
(隐私:探测结果可能进 trace / 报告)。``-shm`` 不可用(第二个连接读不回)时退而
验证单连接 EXCLUSIVE 模式,可用则以 ``locking_mode="EXCLUSIVE"`` 返回成功。

结果按目录缓存(进程内):同一目录的所有 store 共享一次探测。
"""

import sqlite3
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

_BLOB_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """一次探测的结论;``code`` 是失败步骤码(成功为 ``None``),从不含路径。"""

    ok: bool
    code: str | None = None
    locking_mode: Literal["NORMAL", "EXCLUSIVE"] = "NORMAL"


_CACHE_LOCK = Lock()
_CACHE: dict[Path, ProbeResult] = {}

Connect = Callable[[str], sqlite3.Connection]


def _default_connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None, timeout=10.0)


def clear_probe_cache() -> None:
    """清空进程内探测缓存(测试,或目录挂载方式变化之后)。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def probe_directory(
    directory: Path, *, refresh: bool = False, connect: Connect = _default_connect
) -> ProbeResult:
    """``directory`` 上 sqlite(WAL)是否可用;结果按目录缓存。

    ``connect`` 是测试注入损坏连接(如 COMMIT 抛 ``disk I/O error``,模拟 Volumes 式
    随机写不可用)的缝;注入时请配合 ``refresh=True``,注入的结果同样进入缓存。
    """
    key = directory.resolve()
    if not refresh:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            return cached
    result = _probe(directory, connect)
    with _CACHE_LOCK:
        _CACHE[key] = result
    return result


def _probe(directory: Path, connect: Connect) -> ProbeResult:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ProbeResult(False, "sqlite_directory")
    db = directory / f".ragspine-probe-{uuid.uuid4().hex}.sqlite"
    try:
        return _probe_database(db, connect)
    finally:
        _cleanup(db)


def _probe_database(db: Path, connect: Connect) -> ProbeResult:
    try:
        writer = connect(str(db))
    except sqlite3.Error:
        return ProbeResult(False, "sqlite_connect")
    try:
        try:
            writer.execute("PRAGMA page_size = 16384")
            mode = writer.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                return ProbeResult(False, "sqlite_wal")
        except sqlite3.Error:
            return ProbeResult(False, "sqlite_wal")
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("CREATE TABLE probe(key INTEGER PRIMARY KEY, value BLOB NOT NULL)")
            writer.execute("INSERT INTO probe VALUES (1, ?)", (b"\xa5" * _BLOB_BYTES,))
            writer.execute("COMMIT")
        except sqlite3.Error:
            return ProbeResult(False, "sqlite_write")
        if _read_back_second_connection(db, connect):
            try:
                check = writer.execute("PRAGMA quick_check(1)").fetchone()
                if check is None or str(check[0]) != "ok":
                    return ProbeResult(False, "sqlite_quick_check")
            except sqlite3.Error:
                return ProbeResult(False, "sqlite_quick_check")
            try:
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                return ProbeResult(False, "sqlite_checkpoint")
            return ProbeResult(True)
    finally:
        with suppress(sqlite3.Error):
            writer.close()
    # -shm 不可用(第二个连接读不回):写者已关,再试单连接 EXCLUSIVE 候选。
    return _probe_exclusive(db, connect)


def _read_back_second_connection(db: Path, connect: Connect) -> bool:
    """第二个连接读回(验 ``-shm`` / WAL 跨连接可见性)。"""
    try:
        reader = connect(str(db))
        try:
            row = reader.execute("SELECT length(value) FROM probe WHERE key = 1").fetchone()
        finally:
            reader.close()
        return row is not None and int(row[0]) == _BLOB_BYTES
    except sqlite3.Error:
        return False


def _probe_exclusive(db: Path, connect: Connect) -> ProbeResult:
    """``-shm`` 不可用:单连接 EXCLUSIVE + WAL 仍可用吗?(设计稿 §2.1 的候补)"""
    try:
        connection = connect(str(db))
        try:
            connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO probe VALUES (2, ?)", (b"\x5a" * 1024,))
            connection.execute("COMMIT")
            row = connection.execute("SELECT length(value) FROM probe WHERE key = 2").fetchone()
        finally:
            connection.close()
        if row is not None and int(row[0]) == 1024:
            return ProbeResult(True, None, "EXCLUSIVE")
    except sqlite3.Error:
        pass
    return ProbeResult(False, "sqlite_shm")


def _cleanup(db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        with suppress(OSError):
            Path(str(db) + suffix).unlink(missing_ok=True)
