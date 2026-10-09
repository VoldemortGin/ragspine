"""分阶段后端(enterprise-pdf-rag ADR 0044):本地盘 sqlite 工作副本 + 阶段结束整文件发布。

``StagedBackend`` 是 ``SqliteBackend`` 的一个变体:store db 放在本地盘工作目录
(``/tmp``、Databricks 的 ``/local_disk0``),运行期所有 db 读写都只碰本地;``commit()``
用 sqlite 在线备份把 db 的已提交状态拍成一份自足的快照,整文件顺序写到发布目录(store 根,
如 Workspace files 的 FUSE)里的临时名,fsync 后 ``os.replace`` 原子替换 ``store.sqlite``。
外置对象(PDF、超阈值大对象)与旧文件布局的读穿仍直接走发布目录(``FileBackend`` 层不变),
所以 ``object_location`` / ``content_path`` 给出的路径与 store 的 ``root`` 一致。

- **断点续跑**:打开时本地没有工作副本、或发布版自上次提交 / 拷回后被别处换过 → 先把发布版
  整文件拷回本地;发布版没变 → 保留本地(可能带着上次没来得及发布的进度,首次提交补上)。
- **互斥**:第一次写之前在发布目录取 ``store.sqlite.publisher`` 租约(ADR 0023 的持有者
  JSON + 租约 + 接管代次,``lease.py``);另一个活着的进程持有 → ``StoreBusy``。
- **进程内共享**:``acquire_staged`` 按 store 根把实例放进注册表,阶段间(各 stage 各开一个
  store)拿到的是同一个实例;store 的 ``close`` 只放掉自己的引用。``commit_staged`` /
  ``release_staged`` 按目录前缀提交 / 收尾(``folder_pipeline`` 在阶段边界调用);
  进程退出时 ``atexit`` 再提交一次没提交的(失败只计数)。
- **隐私**:``counts`` 只有计数与毫秒;异常只有固定文案 / 原因码,不含路径与正文。

只用于对象 store;模型缓存在 staged 模式下走文件布局(注册表负责)。
"""

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from collections import Counter
from contextlib import closing, suppress
from pathlib import Path
from time import perf_counter
from typing import Literal

from ragspine.common.evidence.file_placement import fsync_directory
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.protocol import (
    BackendKind,
    StageEntry,
    StoreBusy,
)
from ragspine.common.evidence.object_backend.sqlite import (
    DEFAULT_INLINE_MAX_BYTES,
    DEFAULT_MAX_DB_BYTES,
    WRITER_LEASE_SECONDS,
    SqliteBackend,
)

PUBLISHER_CLAIM_FORMAT = "object-store-staged-publisher-v1"
_COPY_CHUNK = 8 * 1024 * 1024
# 一份文件状态:(size, mtime_ns);不存在 → None。FUSE 上 inode 不可信,不纳入。
FileMarks = tuple[int, int] | None


def _marks(path: Path) -> FileMarks:
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_size, status.st_mtime_ns)


# 内容签名:每张表的 (行数, 最近写入时刻)。比文件 stat 准——sqlite 打开 / 检查点会改文件
# 而不改内容;对象的就地修复(UPDATE 不动 created_at)看不出来,那是可重算的缓存。
_SIGNATURE_SQL = (
    "SELECT count(*), max(created_at) FROM objects",
    "SELECT count(*), max(created_at) FROM stage_cache",
    "SELECT count(*), max(updated_at) FROM pointers",
    "SELECT count(*), max(updated_at) FROM records",
)


def _signature(connection: sqlite3.Connection) -> list[list[object]] | None:
    try:
        return [list(connection.execute(sql).fetchone()) for sql in _SIGNATURE_SQL]
    except sqlite3.Error:
        return None


def _copy_whole(source: Path, target: Path) -> int:
    """整文件顺序拷贝到 ``target`` 同目录的临时名,fsync 后原子替换;返回字节数。

    临时名以 ``.<name>.staging-`` 开头:进程在 rename 之前被杀只留下它,``target`` 原样。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.staging-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, _COPY_CHUNK)
            writer.flush()
            os.fsync(writer.fileno())
            size = writer.tell()
        os.replace(temporary, target)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    with suppress(OSError):
        fsync_directory(target.parent)
    return size


class StagedBackend(SqliteBackend):
    """``root`` = 发布目录(store 根);db 在 ``work_dir``;``commit()`` 整文件发布。"""

    kind: BackendKind = "staged"

    def __init__(
        self,
        root: Path,
        *,
        work_dir: Path,
        db_name: str = "store.sqlite",
        synchronous: str = "FULL",
        inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
        external_media_types: frozenset[str] = frozenset({"application/pdf"}),
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    ) -> None:
        super().__init__(
            root,
            db_name=db_name,
            db_path=work_dir / db_name,
            synchronous=synchronous,
            inline_max_bytes=inline_max_bytes,
            external_media_types=external_media_types,
            max_db_bytes=max_db_bytes,
        )
        self.work_dir = work_dir
        self.counts: Counter[str] = Counter()
        self._published = root / db_name
        self._local = work_dir / db_name
        self._marker = work_dir / f"{db_name}.published"
        self._publisher_base = root / f"{db_name}.publisher"
        self._commit_lock = threading.Lock()
        self._publisher = False
        self._refs = 1
        self._closed = False
        # 写入代次:每次写 +1;提交记下拍快照前的代次。不等即"有未发布的写入"。
        self._writes = 0
        self._committed = 0
        self._prepare()

    # ---- 打开:拷回 / 保留本地 -------------------------------------------------------

    def _read_marker(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self._marker.read_bytes())
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_marker(self, signature: list[list[object]] | None) -> None:
        """记下发布版的 (size, mtime_ns) 与它的内容签名(= 本地此刻已发布到哪里)。"""
        payload = {"published": _marks(self._published), "signature": signature}
        temporary = self._marker.with_name(f"{self._marker.name}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self._marker)

    def _local_signature(self) -> list[list[object]] | None:
        with closing(sqlite3.connect(self._local)) as connection:
            return _signature(connection)

    def _prepare(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        published = _marks(self._published)
        marker = self._read_marker() or {}
        recorded = marker.get("published")
        same = isinstance(recorded, list) and tuple(recorded) == published
        if published is not None and (not self._local.is_file() or not same):
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self._local}{suffix}").unlink(missing_ok=True)
            started = perf_counter()
            self.counts["restored_bytes"] += _copy_whole(self._published, self._local)
            self.counts["restores"] += 1
            self.counts["restore_ms"] += round((perf_counter() - started) * 1000)
            self._write_marker(self._local_signature())
            return
        if self._local.is_file() and self._local_signature() != marker.get("signature"):
            self._writes = 1  # 本地带着没发布过的进度(上次进程没走到提交)

    # ---- 写:先取发布者租约,再记代次 ----------------------------------------------------

    def _claim_publisher(self) -> None:
        if self._publisher:
            return
        owner = lease.current_owner(PUBLISHER_CLAIM_FORMAT, WRITER_LEASE_SECONDS)
        content = lease.owner_payload(PUBLISHER_CLAIM_FORMAT, owner)
        if lease.holder_process(self._publisher_base) != lease.PROCESS_TOKEN:
            generation = lease.acquire_lease(
                self._publisher_base, content, claim_format=PUBLISHER_CLAIM_FORMAT
            )
            if generation is None:
                raise StoreBusy("store_busy")
        self._publisher = True

    def put_object(
        self, digest: str, data: bytes, media_type: str, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        self._claim_publisher()
        result = super().put_object(digest, data, media_type, replace=replace)
        if result == "placed":
            self._writes += 1
        return result

    def put_stage_entry(
        self, fingerprint: str, entry: StageEntry, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        self._claim_publisher()
        result = super().put_stage_entry(fingerprint, entry, replace=replace)
        if result == "placed":
            self._writes += 1
        return result

    def set_pointer(self, name: str, digest: str) -> None:
        self._claim_publisher()
        super().set_pointer(name, digest)
        self._writes += 1

    def put_record(self, name: str, data: bytes) -> None:
        self._claim_publisher()
        super().put_record(name, data)
        self._writes += 1

    # ---- 提交与收尾 --------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self._writes != self._committed

    def commit(self) -> bool:
        """把已提交的 db 状态整文件发布到 store 根;没有新写入 → ``False``(什么都不碰)。

        在任何 ``transaction()`` 之外调用(进行中的事务不在快照里,留给下一次提交)。"""
        with self._commit_lock:
            if not self.dirty or not self._local.is_file():
                return False
            seen = self._writes
            started = perf_counter()
            self._claim_publisher()
            for leftover in self.root.glob(f".{self._published.name}.staging-*"):
                with suppress(OSError):
                    leftover.unlink()  # 被杀的提交留下的临时文件(持租约者才清)
            snapshot = self.work_dir / f"{self._published.name}.snapshot"
            snapshot.unlink(missing_ok=True)
            with (
                closing(sqlite3.connect(self._local)) as source,
                closing(sqlite3.connect(snapshot)) as target,
            ):
                source.backup(target)
                target.execute("PRAGMA journal_mode = DELETE")  # 自足的单文件,无 -wal
                signature = _signature(target)
            try:
                self.counts["published_bytes"] += _copy_whole(snapshot, self._published)
            finally:
                snapshot.unlink(missing_ok=True)
            self._committed = seen
            self._write_marker(signature)
            self.counts["commits"] += 1
            self.counts["commit_ms"] += round((perf_counter() - started) * 1000)
            return True

    def close(self) -> None:
        """放掉一个引用;最后一个引用提交(失败只计数,本地副本留给续跑)并释放连接与租约。"""
        with _REGISTRY_LOCK:
            if self._closed:
                return
            self._refs -= 1
            if self._refs > 0:
                return
            self._closed = True
            if _REGISTRY.get(self.root) is self:
                del _REGISTRY[self.root]
        try:
            _commit_quietly(self)
        finally:
            super().close()
            if self._publisher:
                lease.release_lease(self._publisher_base)
                self._publisher = False


# ---- 进程内注册表 ----------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[Path, StagedBackend] = {}


def work_dir_for(root: Path, staging_root: Path) -> Path:
    """store 根的本地工作目录:``<staging_root>/<sha256(根路径) 前 32 位>``(不含路径明文)。"""
    return staging_root / hashlib.sha256(str(root).encode()).hexdigest()[:32]


def acquire_staged(
    root: Path,
    *,
    staging_root: Path,
    synchronous: str = "FULL",
    inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
    external_media_types: frozenset[str] = frozenset({"application/pdf"}),
    max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
) -> StagedBackend:
    """``root`` 的共享实例(注册表持有一个引用,调用方得到另一个,用完 ``close``)。"""
    key = root.expanduser().resolve()
    with _REGISTRY_LOCK:
        backend = _REGISTRY.get(key)
        if backend is None:
            backend = StagedBackend(
                key,
                work_dir=work_dir_for(key, staging_root),
                synchronous=synchronous,
                inline_max_bytes=inline_max_bytes,
                external_media_types=external_media_types,
                max_db_bytes=max_db_bytes,
            )
            _REGISTRY[key] = backend
        backend._refs += 1
        return backend


def _under(prefix: Path) -> list[StagedBackend]:
    base = prefix.expanduser().resolve()
    with _REGISTRY_LOCK:
        return [backend for key, backend in _REGISTRY.items() if key.is_relative_to(base)]


def registered() -> tuple[Path, ...]:
    """注册表里的 store 根(测试 / 诊断用)。"""
    with _REGISTRY_LOCK:
        return tuple(sorted(_REGISTRY))


def commit_staged(prefix: Path) -> int:
    """提交 ``prefix`` 之下所有有新写入的实例(阶段边界);返回提交了几个。错误照抛。"""
    return sum(backend.commit() for backend in _under(prefix))


def release_staged(prefix: Path) -> None:
    """``prefix`` 之下的实例:提交(失败只计数,本地副本留给续跑),离开注册表并放掉注册表的
    引用(文档结束;还有 store 开着时由最后一个 ``close`` 收尾)。"""
    for backend in _under(prefix):
        with _REGISTRY_LOCK:
            if _REGISTRY.get(backend.root) is not backend:
                continue
            del _REGISTRY[backend.root]
        _commit_quietly(backend)
        backend.close()


def _commit_quietly(backend: StagedBackend) -> None:
    try:
        backend.commit()
    except (OSError, sqlite3.Error, StoreBusy):
        backend.counts["commit_failures"] += 1


@atexit.register
def _commit_at_exit() -> None:
    for backend in _under(Path("/")):
        _commit_quietly(backend)
