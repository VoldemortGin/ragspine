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
  进程退出时 ``atexit`` 提交没提交的(失败只计数)并收尾(连接、发布者租约)。
- **隐私**:``counts`` 只有计数与毫秒;异常只有固定文案 / 原因码,不含路径与正文。

``StagedModelCacheBackend``(enterprise-pdf-rag ADR 0046):文档自己的模型缓存
(``<processing store>/model-cache``)同样在本地读写、随文档的 store 在阶段边界整文件发布成
``model-cache/model-cache.sqlite``;它的"有没有新写入"看内容签名(claim 进出不算)。
根级答案缓存(多进程写)不在此列,仍走文件布局(注册表负责)。staged 对象 store 默认把
内联上限提到 ``STAGED_INLINE_MAX_BYTES``(8 MiB;显式设置仍优先),大 SVG 也压进 db。

``StagedDocument``(enterprise-pdf-rag ADR 0047):每份文档一个 db。``<doc>/source``、
``<doc>/processing`` 与 ``<doc>/processing/model-cache`` 是同一个本地 db 的三个 scope
(``StagedDocumentBackend`` / ``StagedDocumentModelCache``,共用连接、事务与写者租约),
整文件发布成 ``<doc>/document.sqlite``,一个发布者租约 ``document.sqlite.publisher``。
PDF 原件也进库(``STAGED_PDF_INLINE_MAX_BYTES``)。旧的每 store 一个 db 的布局 →
``LayoutMismatch``(不读、不改、不迁移)。
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
    LayoutMismatch,
    StageEntry,
    StoreBusy,
)
from ragspine.common.evidence.object_backend.sqlite import (
    DEFAULT_INLINE_MAX_BYTES,
    DEFAULT_MAX_DB_BYTES,
    DOCUMENT_SCHEMA,
    DOCUMENT_SCOPES,
    WRITER_LEASE_SECONDS,
    SqliteBackend,
    SqliteModelCacheBackend,
    _SqliteCore,
)

PUBLISHER_CLAIM_FORMAT = "object-store-staged-publisher-v1"
# staged 对象 store 的默认内联上限(ADR 0046):发布目录只留整文件;db 超过
# ``max_db_bytes`` 后 > 16 KiB 的新对象仍外置(ADR 0036 的保护阈)。
STAGED_INLINE_MAX_BYTES = 8 * 1024 * 1024
# 每份文档一个 db(ADR 0047):发布名,与 PDF 原件的内联上限(过了它 PDF 仍外置成文件)。
DOCUMENT_DB_NAME = "document.sqlite"
STAGED_PDF_INLINE_MAX_BYTES = 64 * 1024 * 1024
# 旧布局(ADR 0044 / 0046 的每 store 一个 db,或 sqlite 后端):文档目录里有任一个 → 布局不符。
LEGACY_DOCUMENT_DBS = (
    "source/store.sqlite",
    "processing/store.sqlite",
    "processing/model-cache/model-cache.sqlite",
)
_LEGACY_LAYOUT = (
    "layout_mismatch: 这个文档目录是旧布局写的(每个 store 根一个 store.sqlite /"
    " model-cache.sqlite:ADR 0044/0046 的 staged 或 sqlite 后端);staged 现在每份文档只有一个"
    " document.sqlite(ADR 0047),不读也不改旧文件。请删掉这个文档目录后重跑(模型调用会重做),"
    "或改回写它时的 APP_OBJECT_STORE_BACKEND。"
)
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
# 模型缓存的内容签名:claims 是运行期互斥,不算内容(只有 claim 进出不重发)。
_MODEL_CACHE_SIGNATURE_SQL = (
    "SELECT count(*), max(created_at) FROM requests",
    "SELECT count(*), max(created_at) FROM responses",
    "SELECT count(*), max(created_at) FROM contexts",
)
# 文档 db 的内容签名:共用对象 + 每个 scope 的成员 / stage-cache / 指针 / records + 模型缓存。
_DOCUMENT_SIGNATURE_SQL = (
    "SELECT count(*), max(created_at) FROM objects",
    *(
        sql
        for scope in DOCUMENT_SCOPES
        for sql in (
            f"SELECT count(*), NULL FROM {scope}_object_refs",
            f"SELECT count(*), max(created_at) FROM {scope}_stage_cache",
            f"SELECT count(*), max(updated_at) FROM {scope}_pointers",
            f"SELECT count(*), max(updated_at) FROM {scope}_records",
        )
    ),
    *_MODEL_CACHE_SIGNATURE_SQL,
)


def _signature(
    connection: sqlite3.Connection, statements: tuple[str, ...] = _SIGNATURE_SQL
) -> list[list[object]] | None:
    try:
        return [list(connection.execute(sql).fetchone()) for sql in statements]
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


class _StagedDb:
    """一份本地工作 db 与它在发布目录里的整文件:拷回 / 内容签名 / 发布者租约 / 整文件发布 /
    引用计数收尾。对象 store 与文档模型缓存共用(``_signature_sql`` 各自的表)。"""

    _signature_sql: tuple[str, ...] = _SIGNATURE_SQL

    def _setup_staging(self, published_dir: Path, work_dir: Path, db_name: str) -> None:
        self.work_dir = work_dir
        self.counts: Counter[str] = Counter()
        self._key = published_dir  # 注册表的键 = 发布目录
        self._published = published_dir / db_name
        self._local = work_dir / db_name
        self._marker = work_dir / f"{db_name}.published"
        self._publisher_base = published_dir / f"{db_name}.publisher"
        self._commit_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._publisher = False
        self._refs = 1
        self._closed = False
        # 写入代次:每次写 +1;提交记下拍快照前的代次。不等即"有未发布的写入"。
        self._writes = 0
        self._committed = 0
        self._prepare()

    def _close_db(self) -> None:
        raise NotImplementedError

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
            return _signature(connection, self._signature_sql)

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
        # 锁内判断:文档内页级并发(ADR 0045)的几个线程可能同时首写,只取一次——否则后来的
        # 线程看见本进程刚写下的活租约,会被当成"别人持有"而 StoreBusy。
        with self._claim_lock:
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

    # ---- 提交与收尾 --------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self._writes != self._committed

    def commit(self) -> bool:
        """把已提交的 db 状态整文件发布到发布目录;没有新写入 → ``False``(什么都不碰)。

        在任何 ``transaction()`` 之外调用(进行中的事务不在快照里,留给下一次提交)。"""
        with self._commit_lock:
            if not self._local.is_file() or not self.dirty:
                return False
            seen = self._writes
            started = perf_counter()
            self._claim_publisher()
            for leftover in self._key.glob(f".{self._published.name}.staging-*"):
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
                signature = _signature(target, self._signature_sql)
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
            if _REGISTRY.get(self._key) is self:
                del _REGISTRY[self._key]
        self._finish()

    def _finish(self) -> None:
        """提交(失败只计数)、关连接(检查点 TRUNCATE)、删发布者租约;只做一次。"""
        with _REGISTRY_LOCK:
            if self._closed:
                return
            self._closed = True
        try:
            _commit_quietly(self)
        finally:
            self._close_db()
            if self._publisher:
                lease.release_lease(self._publisher_base)
                self._publisher = False


class StagedBackend(_StagedDb, SqliteBackend):
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
        SqliteBackend.__init__(
            self,
            root,
            db_name=db_name,
            db_path=work_dir / db_name,
            synchronous=synchronous,
            inline_max_bytes=inline_max_bytes,
            external_media_types=external_media_types,
            max_db_bytes=max_db_bytes,
        )
        self._setup_staging(root, work_dir, db_name)

    def _close_db(self) -> None:
        SqliteBackend.close(self)

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


class StagedModelCacheBackend(_StagedDb, SqliteModelCacheBackend):
    """文档自己的模型缓存(ADR 0046):``cache_dir`` = 发布目录;db 在 ``work_dir``。

    claim / 单飞 / 预算 / 429 冷却的语义与 ``SqliteModelCacheBackend`` 相同(同一份代码,
    只是 db 在本地);"有没有新写入"看内容签名(请求记录 / 响应 / 上下文),所以只读回放、
    只有 claim 进出的一轮不重发。发布者租约在第一次发布前取。"""

    kind: BackendKind = "staged"
    _signature_sql = _MODEL_CACHE_SIGNATURE_SQL

    def __init__(
        self,
        cache_dir: Path,
        *,
        work_dir: Path,
        db_name: str = "model-cache.sqlite",
        synchronous: str = "FULL",
    ) -> None:
        SqliteModelCacheBackend.__init__(
            self,
            cache_dir,
            db_name=db_name,
            db_path=work_dir / db_name,
            synchronous=synchronous,
        )
        self._setup_staging(cache_dir, work_dir, db_name)

    @property
    def dirty(self) -> bool:
        if not self._local.is_file():
            return False
        recorded = (self._read_marker() or {}).get("signature")
        return self._local_signature() != recorded

    def _close_db(self) -> None:
        SqliteModelCacheBackend.close(self)


class StagedDocument(_StagedDb):
    """每份文档一个 db(ADR 0047):本地工作副本 ``<work_dir>/document.sqlite``,整文件发布成
    ``<root>/document.sqlite``(``root`` = 文档目录)。两个对象 store 与模型缓存是它的 scope,
    共用一个 ``_SqliteCore``——同一线程同一连接,跨 scope 的嵌套事务就是同一个事务;写者租约
    按进程持有(模型缓存也是:一份文档只有持发布者租约的进程写)。"有没有新写入"= 写入代次
    或内容签名(模型缓存的写不记代次)。"""

    _signature_sql = _DOCUMENT_SIGNATURE_SQL

    def __init__(self, root: Path, *, work_dir: Path, synchronous: str = "FULL") -> None:
        if any((root / name).is_file() for name in LEGACY_DOCUMENT_DBS):
            raise LayoutMismatch(_LEGACY_LAYOUT)
        self.root = root
        self.core = _SqliteCore(
            work_dir / DOCUMENT_DB_NAME, DOCUMENT_SCHEMA, synchronous=synchronous
        )
        self._scopes: dict[str, StagedDocumentBackend] = {}
        self._model_cache: StagedDocumentModelCache | None = None
        self._setup_staging(root, work_dir, DOCUMENT_DB_NAME)

    def object_scope(
        self,
        scope: str,
        root: Path | None = None,
        *,
        inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
        external_media_types: frozenset[str] = frozenset({"application/pdf"}),
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        media_inline_max_bytes: dict[str, int] | None = None,
    ) -> "StagedDocumentBackend":
        """``scope`` 的对象 store(每个 scope 一个实例;``root`` 缺省 = ``<文档目录>/<scope>``,
        外置对象与旧文件布局在那里)。不加引用:引用由调用方(注册表)记。"""
        backend = self._scopes.get(scope)
        if backend is None:
            backend = StagedDocumentBackend(
                self,
                scope,
                self.root / scope if root is None else root,
                inline_max_bytes=inline_max_bytes,
                external_media_types=external_media_types,
                max_db_bytes=max_db_bytes,
                media_inline_max_bytes=media_inline_max_bytes,
            )
            self._scopes[scope] = backend
        return backend

    def model_cache_scope(self, cache_dir: Path | None = None) -> "StagedDocumentModelCache":
        """文档的模型缓存(``cache_dir`` 缺省 = ``<文档目录>/processing/model-cache``)。"""
        if self._model_cache is None:
            self._model_cache = StagedDocumentModelCache(
                self, self.root / "processing" / "model-cache" if cache_dir is None else cache_dir
            )
        return self._model_cache

    @property
    def dirty(self) -> bool:
        if self._writes != self._committed:
            return True
        if not self._local.is_file():
            return False
        return self._local_signature() != (self._read_marker() or {}).get("signature")

    def _close_db(self) -> None:
        self.core.close()


class StagedDocumentBackend(SqliteBackend):
    """文档 db 里的一个对象 store scope(``source`` / ``processing``)。``commit`` / ``close`` /
    ``counts`` / ``work_dir`` 都是文档的;写之前取文档的发布者租约。"""

    kind: BackendKind = "staged"

    def __init__(
        self,
        document: StagedDocument,
        scope: str,
        root: Path,
        *,
        inline_max_bytes: int,
        external_media_types: frozenset[str],
        max_db_bytes: int,
        media_inline_max_bytes: dict[str, int] | None,
    ) -> None:
        SqliteBackend.__init__(
            self,
            root,
            inline_max_bytes=inline_max_bytes,
            external_media_types=external_media_types,
            max_db_bytes=max_db_bytes,
            media_inline_max_bytes=media_inline_max_bytes,
            core=document.core,
            scope=scope,
        )
        self.document = document

    @property
    def work_dir(self) -> Path:
        return self.document.work_dir

    @property
    def counts(self) -> Counter[str]:
        return self.document.counts

    def commit(self) -> bool:
        return self.document.commit()

    def close(self) -> None:
        self.document.close()

    def put_object(
        self, digest: str, data: bytes, media_type: str, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        self.document._claim_publisher()
        result = super().put_object(digest, data, media_type, replace=replace)
        if result == "placed":
            self.document._writes += 1
        return result

    def put_stage_entry(
        self, fingerprint: str, entry: StageEntry, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        self.document._claim_publisher()
        result = super().put_stage_entry(fingerprint, entry, replace=replace)
        if result == "placed":
            self.document._writes += 1
        return result

    def set_pointer(self, name: str, digest: str) -> None:
        self.document._claim_publisher()
        super().set_pointer(name, digest)
        self.document._writes += 1

    def put_record(self, name: str, data: bytes) -> None:
        self.document._claim_publisher()
        super().put_record(name, data)
        self.document._writes += 1


class StagedDocumentModelCache(SqliteModelCacheBackend):
    """文档 db 里的模型缓存 scope:claim / 单飞 / 预算 / 429 冷却与 ``SqliteModelCacheBackend``
    同一份代码;写者租约按进程(与同文档的 store 共用);发布者租约在第一次发布前取(同 ADR 0046)。"""

    kind: BackendKind = "staged"

    def __init__(self, document: StagedDocument, cache_dir: Path) -> None:
        SqliteModelCacheBackend.__init__(self, cache_dir, core=document.core)
        self.document = document

    @property
    def work_dir(self) -> Path:
        return self.document.work_dir

    @property
    def counts(self) -> Counter[str]:
        return self.document.counts

    def commit(self) -> bool:
        return self.document.commit()

    def close(self) -> None:
        self.document.close()


# ---- 进程内注册表 ----------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[Path, _StagedDb] = {}


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
        assert isinstance(backend, StagedBackend)
        return backend


def document_root_of(root: Path, kind: Literal["object", "model-cache"]) -> tuple[Path, str] | None:
    """``root`` 是一份文档的哪个 scope(ADR 0047):``<doc>/source`` / ``<doc>/processing`` →
    (``<doc>``, scope);``<doc>/processing/model-cache`` → (``<doc>``, ``"model-cache"``);
    别的目录(根级答案缓存、任意 store 根)→ ``None``。"""
    if kind == "object":
        return (root.parent, root.name) if root.name in DOCUMENT_SCOPES else None
    if root.name == "model-cache" and root.parent.name == "processing":
        return root.parent.parent, "model-cache"
    return None


def acquire_staged_document(
    root: Path,
    scope: str,
    *,
    staging_root: Path,
    synchronous: str = "FULL",
    inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
    external_media_types: frozenset[str] = frozenset(),
    max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    media_inline_max_bytes: dict[str, int] | None = None,
) -> StagedDocumentBackend:
    """文档目录 ``root`` 的共享 ``StagedDocument`` 的 ``scope``(注册表持有文档的一个引用,
    调用方得到另一个,用完 ``close``)。旧布局 → ``LayoutMismatch``(什么都不登记)。"""
    key = root.expanduser().resolve()
    with _REGISTRY_LOCK:
        document = _REGISTRY.get(key)
        if document is None:
            document = StagedDocument(
                key, work_dir=work_dir_for(key, staging_root), synchronous=synchronous
            )
            _REGISTRY[key] = document
        assert isinstance(document, StagedDocument)
        backend = document.object_scope(
            scope,
            inline_max_bytes=inline_max_bytes,
            external_media_types=external_media_types,
            max_db_bytes=max_db_bytes,
            media_inline_max_bytes=media_inline_max_bytes,
        )
        document._refs += 1
        return backend


def acquire_staged_model_cache(
    cache_dir: Path, *, synchronous: str = "FULL"
) -> StagedModelCacheBackend | StagedDocumentModelCache | None:
    """``cache_dir`` 的父目录是本进程正在 staged 的 store 根(文档的 processing store)→
    共享的 staged 模型缓存(本地副本与那个 store 同一 staging 根);否则 ``None``(调用方
    走文件布局,如根级答案缓存)。注册表持有唯一的引用:模型缓存的使用者(``JsonCompletionClient``)
    从不 close,由文档结束时的 ``release_staged`` 收尾。"""
    key = cache_dir.expanduser().resolve()
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if isinstance(existing, StagedModelCacheBackend):
            return existing
        located = document_root_of(key, "model-cache")
        document = None if located is None else _REGISTRY.get(located[0])
        if isinstance(document, StagedDocument):
            return document.model_cache_scope(key)  # ADR 0047:文档 db 的模型缓存 scope
        owner = _REGISTRY.get(key.parent)
        if not isinstance(owner, StagedBackend):
            return None
        backend = StagedModelCacheBackend(
            key, work_dir=work_dir_for(key, owner.work_dir.parent), synchronous=synchronous
        )
        _REGISTRY[key] = backend
        return backend


def _under(prefix: Path) -> list[_StagedDb]:
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
            if _REGISTRY.get(backend._key) is not backend:
                continue
            del _REGISTRY[backend._key]
        _commit_quietly(backend)
        backend.close()


def _commit_quietly(backend: _StagedDb) -> None:
    try:
        backend.commit()
    except (OSError, sqlite3.Error, StoreBusy):
        backend.counts["commit_failures"] += 1


@atexit.register
def _finish_at_exit() -> None:
    """进程退出:提交并收尾所有还登记着的实例(连接检查点、发布者租约删除)。"""
    for backend in _under(Path("/")):
        with _REGISTRY_LOCK:
            if _REGISTRY.get(backend._key) is backend:
                del _REGISTRY[backend._key]
        backend._finish()
