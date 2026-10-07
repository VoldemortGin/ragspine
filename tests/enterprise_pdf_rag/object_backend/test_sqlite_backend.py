"""SqliteBackend 专项:连接、并发、崩溃注入、损坏重建、版本门、保护阈与写者租约。"""

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from ragspine.common.evidence.file_placement import recording_repairs
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.protocol import (
    BackendSchemaError,
    StoreBusy,
)
from ragspine.common.evidence.object_backend.sqlite import (
    SqliteBackend,
    SqliteModelCacheBackend,
)
from tests.enterprise_pdf_rag.object_backend.conftest import live_owner, sha

# ---- 线程本地连接与事务 -------------------------------------------------------------------


def test_connections_are_thread_local(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    seen: dict[str, object] = {}

    def record(name: str) -> None:
        seen[name] = backend._core.connection()

    record("main")
    worker = threading.Thread(target=record, args=("worker",))
    worker.start()
    worker.join()
    assert seen["main"] is backend._core.connection()
    assert seen["main"] is not seen["worker"]
    backend.close()


def test_transaction_rolls_back_on_error_and_reenters(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    data = b"rolled back"
    digest = sha(data)
    with pytest.raises(RuntimeError, match="boom"), backend.transaction():
        with backend.transaction():  # 重入:同一外层事务
            backend.put_object(digest, data, "text/plain")
        assert backend.get_object(digest) == data  # 事务内可见
        raise RuntimeError("boom")
    assert backend.get_object(digest) is None  # 外层回滚把整组写入收回
    with backend.transaction():
        backend.put_object(digest, data, "text/plain")
    assert backend.get_object(digest) == data
    backend.close()


# ---- claims:两线程屏障恰好一个赢;os._exit 子进程的 claim 被接管 -------------------------


def test_two_threads_behind_a_barrier_exactly_one_claims(tmp_path: Path) -> None:
    cache = SqliteModelCacheBackend(tmp_path)
    key = "a" * 64
    barrier = threading.Barrier(2)
    results: list[int | None] = []
    lock = threading.Lock()

    def contend() -> None:
        owner = live_owner()
        barrier.wait()
        won = cache.claim(key, owner)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results, key=str) == [0, None] or sorted(results, key=str) == [None, 0]
    cache.close()


_CHILD_CLAIM = """
import os, sys
from pathlib import Path
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.sqlite import SqliteModelCacheBackend

cache = SqliteModelCacheBackend(Path(sys.argv[1]))
owner = lease.current_owner("json-completion-claim-v2", 300)
assert cache.claim("{key}", owner) == 0
os._exit(0)  # 既不 release 也不 close:死进程留下 claim 行与写者租约
"""


def test_dead_child_claim_is_taken_over(tmp_path: Path) -> None:
    key = "c" * 64
    subprocess.run(
        [sys.executable, "-c", _CHILD_CLAIM.replace("{key}", key), str(tmp_path)],
        check=True,
        timeout=60,
    )
    cache = SqliteModelCacheBackend(tmp_path)
    # 子进程已死(POSIX 下按 pid 判定),它的 claim 行被代次 +1 接管;
    # 它留下的 <db>.writer 租约同样被接管(写路径先取写者租约)。
    assert cache.claim(key, live_owner()) == 1
    cache.close()


# ---- WAL 尾帧截断与损坏 db 重建 -----------------------------------------------------------


def test_truncated_wal_tail_loses_only_the_last_transaction(tmp_path: Path) -> None:
    source = tmp_path / "source"
    backend = SqliteBackend(source)
    first, second = b"first transaction", b"second transaction"
    with backend.transaction():
        backend.put_object(sha(first), first, "text/plain")
    with backend.transaction():
        backend.put_object(sha(second), second, "text/plain")
    # 不 close(close 会 checkpoint):模拟进程被杀,db + 热 WAL 原样拷走。
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    for suffix in ("", "-wal"):
        shutil.copy2(f"{source / 'store.sqlite'}{suffix}", f"{crashed / 'store.sqlite'}{suffix}")
    wal = crashed / "store.sqlite-wal"
    payload = wal.read_bytes()
    wal.write_bytes(payload[: len(payload) - 200])  # 撕掉尾部:最后的事务帧不完整
    backend.close()

    recovered = SqliteBackend(crashed)
    with recording_repairs() as repairs:
        assert recovered.get_object(sha(first)) == first  # 更早的事务原样在
        assert recovered.get_object(sha(second)) is None  # 只丢最后一个事务
    assert repairs["store_db"] == 0  # 主库未损坏,不触发重建
    recovered.close()


def test_corrupt_db_is_renamed_and_rebuilt(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    data = b"will be lost with the corrupt db"
    backend.put_object(sha(data), data, "text/plain")
    backend.close()
    db = tmp_path / "store.sqlite"
    payload = bytearray(db.read_bytes())
    payload[0:16] = b"torn by a flush\x00"  # 连文件头都不是 sqlite 的了
    db.write_bytes(bytes(payload))

    with recording_repairs() as repairs:
        reopened = SqliteBackend(tmp_path)
        assert reopened.get_object(sha(data)) is None  # 重建后的空库
        fresh = b"written after the rebuild"
        reopened.put_object(sha(fresh), fresh, "text/plain")
        assert reopened.get_object(sha(fresh)) == fresh
    assert repairs["store_db"] == 1
    corpses = list(tmp_path.glob("store.sqlite.corrupt-*"))
    assert len(corpses) == 1 and corpses[0].stat().st_size > 0
    reopened.close()


def test_quick_check_runs_once_on_open_and_keeps_an_intact_db(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    data = b"intact"
    backend.put_object(sha(data), data, "text/plain")
    backend.close()
    with recording_repairs() as repairs:
        reopened = SqliteBackend(tmp_path)
        assert reopened.get_object(sha(data)) == data
    assert repairs["store_db"] == 0
    assert list(tmp_path.glob("*.corrupt-*")) == []
    reopened.close()


# ---- 版本门 -------------------------------------------------------------------------------


def test_foreign_application_id_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("PRAGMA application_id = 12345")
        connection.execute("CREATE TABLE theirs(x)")
        connection.commit()
    backend = SqliteBackend(tmp_path)
    with pytest.raises(BackendSchemaError, match="application_id"):
        backend.get_object("a" * 64)
    backend.close()


def test_newer_user_version_is_refused(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    data = b"v1"
    backend.put_object(sha(data), data, "text/plain")
    backend.close()
    with closing(sqlite3.connect(tmp_path / "store.sqlite")) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    reopened = SqliteBackend(tmp_path)
    with pytest.raises(BackendSchemaError, match="user_version"):
        reopened.get_object(sha(data))
    reopened.close()


# ---- 400 MB 保护阈 ------------------------------------------------------------------------


def test_db_over_the_size_threshold_externalizes_large_objects(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path, max_db_bytes=1)  # 任何 db 都"超限"
    small = b"small enough to stay inline"
    big = os.urandom(20_000)  # > 16 KiB:超限后一律外置
    backend.put_object(sha(small), small, "text/plain")
    backend.put_object(sha(big), big, "application/octet-stream")
    assert backend.get_object(sha(small)) == small
    assert backend.get_object(sha(big)) == big
    external = tmp_path / "objects" / "sha256-sharded" / sha(big)[:2] / sha(big)
    assert external.read_bytes() == big
    assert not (tmp_path / "objects" / "sha256-sharded" / sha(small)[:2] / sha(small)).exists()
    backend.close()


# ---- 写者租约:活着的别进程持有者 → StoreBusy ---------------------------------------------


def test_live_foreign_writer_lease_means_store_busy(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path)
    data = b"pre-lease write"
    backend.put_object(sha(data), data, "text/plain")  # 本进程先取得租约
    backend.close()  # 释放
    owner = live_owner(3600)
    foreign = lease.owner_payload(
        "object-store-writer-v1",
        type(owner)(owner.host, owner.pid, "another-process-token", owner.created_at, 3600),
    )
    lease.write_lease(tmp_path / "store.sqlite.writer", foreign)
    blocked = SqliteBackend(tmp_path)
    with pytest.raises(StoreBusy, match="store_busy"):
        blocked.put_object(sha(b"nope"), b"nope", "text/plain")
    assert blocked.get_object(sha(data)) == data  # 读者不取租约
    blocked.close()
