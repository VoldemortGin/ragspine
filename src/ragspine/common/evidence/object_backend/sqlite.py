"""sqlite 对象库后端:每个 store 根一个 db,小对象内联、大对象外置、读穿旧文件布局。

设计稿 §2(schema 与 PRAGMA)、§3(等价映射)、§4(并发)、§5(兼容读顺序)的实现:

- 写一律进 db(``INSERT OR IGNORE`` + 读回校验 + 修复);读顺序 db → sharded 文件 →
  flat 文件(``FileBackend`` 作为只读 legacy 层组合进来,外置大对象也经它落盘);
- 同进程多线程:线程本地连接池;事务作用域是可重入的 contextvar;
- 多进程:不信任 FUSE 上的 sqlite 文件锁,写者互斥用 O_EXCL 的 ``<db>.writer``
  租约文件(``lease.py``,复用 ADR 0023 的持有者 JSON + 租约 + 接管代次),
  拿不到 → ``StoreBusy``。store db 的租约按进程持有(文档级 db 只有一个写者进程);
  模型缓存 db 按事务持有(根级共享缓存会被多个进程轮流写,见 ``SqliteModelCacheBackend``);
- WAL + ``synchronous``(默认 FULL);撕裂尾帧由 sqlite 丢弃 = 只丢最后的事务;
  损坏 db 在打开时 ``quick_check`` 失败 → 改名 ``.corrupt-<utc>`` + 重建空库 +
  ``note_repair("store_db")``(ADR 0029 的自愈语义);
- ``application_id`` / ``user_version`` 版本门:不是本代码可写的版本 → ``BackendSchemaError``;
- 400 MiB 保护阈:db 超限后 > 16 KiB 的新对象一律外置。

digest 永远是**解压后**字节的 sha256;zlib(level 6)只用于 ≥ 1 KiB 的
JSON / text / SVG 内联字节。隐私:本模块的异常与 trace 相关值只含 code / 计数,
从不含对象正文。
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import zlib
from _thread import LockType
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic, sleep, time
from typing import Literal

from ragspine.common.evidence.file_placement import note_repair
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import (
    CLAIM_FORMAT,
    FileBackend,
    FileModelCacheBackend,
    _artifact_digest,
)
from ragspine.common.evidence.object_backend.protocol import (
    BackendKind,
    BackendSchemaError,
    ClaimOwner,
    DamagedEntry,
    PinToken,
    StageEntry,
    StoreBusy,
    StoreConflict,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
# "RSP1":ragspine store v1。打开非 0 且非此值的 db 即拒绝(不是我们的文件)。
APPLICATION_ID = 0x52535031
USER_VERSION = 1
PAGE_SIZE = 16384
DEFAULT_INLINE_MAX_BYTES = 262_144
DEFAULT_MAX_DB_BYTES = 400 * 1024 * 1024
# 超过保护阈后仍可内联的小对象上限(设计稿 §6)。
_OVERFLOW_INLINE_MAX = 16_384
_COMPRESS_MIN = 1024
# 批量校验(verify_many)单条 IN 查询的摘要数上限(设计稿 §3 的 ADR 0024 批量读)。
_VERIFY_BATCH = 500
# 写者租约:持有者是进程而不是单次调用,租期取顶格调用租约(840 s)之上的整小时;
# 持有者死亡仍由 pid / 租约规则接管(lease.lease_expired)。
WRITER_LEASE_SECONDS = 3600
WRITER_CLAIM_FORMAT = "object-store-writer-v1"
# 按事务持有的写者租约(模型缓存 db):持有者只在一个毫秒级事务里拿着它;等它的时长与
# ``busy_timeout`` 相同,租期远长于任何事务(持有者死了照样按 pid 立即接管)。
TRANSACTION_LEASE_SECONDS = 120
TRANSACTION_LEASE_WAIT_SECONDS = 30.0
WriterScope = Literal["process", "transaction"]
# claims 行的 (claim, host, pid, process, created_at, lease_seconds)。
_ClaimColumns = tuple[str, str, int, str, float, int]

STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(
  digest TEXT NOT NULL PRIMARY KEY CHECK(length(digest)=64),
  byte_length INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  encoding TEXT NOT NULL DEFAULT 'raw',
  external INTEGER NOT NULL DEFAULT 0,
  bytes BLOB,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stage_cache(
  fingerprint TEXT NOT NULL PRIMARY KEY CHECK(length(fingerprint)=64),
  envelope_digest TEXT NOT NULL,
  envelope BLOB NOT NULL,
  stage TEXT NOT NULL,
  producer TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  product BLOB,
  product_encoding TEXT NOT NULL DEFAULT 'raw',
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS stage_cache_by_artifact ON stage_cache(artifact_digest);
CREATE TABLE IF NOT EXISTS pointers(name TEXT PRIMARY KEY, digest TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS records(name TEXT PRIMARY KEY, bytes BLOB NOT NULL, updated_at REAL NOT NULL);
"""

MODEL_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(
  record_key TEXT NOT NULL PRIMARY KEY,
  request_fingerprint TEXT NOT NULL,
  record BLOB NOT NULL,
  response_digest TEXT,
  failure_code TEXT,
  provider_error_param TEXT,
  provider_error_code TEXT,
  claim_takeover INTEGER,
  attempt INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS requests_by_fingerprint ON requests(request_fingerprint);
CREATE TABLE IF NOT EXISTS responses(
  digest TEXT PRIMARY KEY,
  byte_length INTEGER NOT NULL,
  encoding TEXT NOT NULL DEFAULT 'raw',
  bytes BLOB NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS contexts(
  request_fingerprint TEXT PRIMARY KEY,
  encoding TEXT NOT NULL DEFAULT 'raw',
  bytes BLOB NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
  record_key TEXT NOT NULL PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 0,
  claim TEXT NOT NULL,
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  process TEXT NOT NULL,
  created_at REAL NOT NULL,
  lease_seconds INTEGER NOT NULL);
"""

# 每份文档一个 db(enterprise-pdf-rag ADR 0047,staged 模式):两个对象 store 是 db 里的两个
# scope。内容寻址的 ``objects`` 共用(同 digest 只存一行字节),每个 scope 用自己的成员表
# ``<scope>_object_refs`` 与只读视图 ``<scope>_objects`` 读;stage-cache / 指针 / records 各一份
# 带 scope 前缀的表。文档的模型缓存表(requests / responses / contexts / claims)独此一份。
DOCUMENT_SCOPES = ("source", "processing")
_SCOPED_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {scope}_object_refs(digest TEXT NOT NULL PRIMARY KEY);
CREATE VIEW IF NOT EXISTS {scope}_objects AS SELECT objects.* FROM objects
  JOIN {scope}_object_refs ON {scope}_object_refs.digest = objects.digest;
CREATE TABLE IF NOT EXISTS {scope}_stage_cache(
  fingerprint TEXT NOT NULL PRIMARY KEY CHECK(length(fingerprint)=64),
  envelope_digest TEXT NOT NULL,
  envelope BLOB NOT NULL,
  stage TEXT NOT NULL,
  producer TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  product BLOB,
  product_encoding TEXT NOT NULL DEFAULT 'raw',
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS {scope}_stage_cache_by_artifact ON {scope}_stage_cache(artifact_digest);
CREATE TABLE IF NOT EXISTS {scope}_pointers(name TEXT PRIMARY KEY, digest TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS {scope}_records(name TEXT PRIMARY KEY, bytes BLOB NOT NULL, updated_at REAL NOT NULL);
"""
_OBJECTS_TABLE = STORE_SCHEMA[STORE_SCHEMA.index("CREATE TABLE IF NOT EXISTS objects(") :].split(
    ";\n", 1
)[0]
DOCUMENT_SCHEMA = (
    "\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
    + _OBJECTS_TABLE
    + ";\n"
    + "".join(_SCOPED_STORE_SCHEMA.lstrip("\n").format(scope=scope) for scope in DOCUMENT_SCOPES)
    + MODEL_CACHE_SCHEMA.split(";\n", 1)[1]
)


@dataclass(frozen=True, slots=True)
class _Tables:
    """一个对象 store 读写的表名。缺省 = 每个 store 根一个 db 的原表名(SQL 逐字不变);
    文档 db 的 scope:读对象走视图、写对象进共用 ``objects`` 并记成员行。"""

    objects: str = "objects"
    refs: str | None = None
    stage_cache: str = "stage_cache"
    pointers: str = "pointers"
    records: str = "records"

    @classmethod
    def scoped(cls, scope: str) -> "_Tables":
        if scope not in DOCUMENT_SCOPES:
            raise ValueError("Unknown document scope")
        return cls(
            f"{scope}_objects",
            f"{scope}_object_refs",
            f"{scope}_stage_cache",
            f"{scope}_pointers",
            f"{scope}_records",
        )


# 本进程已持有的写者租约:db 路径 → 持有它的后端实例数(同进程可重入,见 §4)。
_WRITER_LOCK = Lock()
_WRITER_COUNTS: dict[Path, int] = {}
# 按事务持有租约的 db:同进程的事务先在这把(按 db 路径的)锁上排队,再取文件租约。
_TRANSACTION_LOCKS: dict[Path, LockType] = {}


def _transaction_lock(db_path: Path) -> LockType:
    with _WRITER_LOCK:
        return _TRANSACTION_LOCKS.setdefault(db_path, Lock())


def _require_digest(digest: str) -> None:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("Invalid content-addressed artifact identifier")


_CORRUPTION_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
# 与 ``PRAGMA busy_timeout = 30000`` 同长。
_BUSY_SECONDS = 30.0


def _busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_CODES


def _corrupt(error: sqlite3.DatabaseError) -> bool:
    """这个错误说的是"db 文件坏了"(而不是忙 / 锁 / I/O 等可重试的状况)吗?
    本模块自己抛的 quick_check 失败不带错误码,算损坏。"""
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return True
    return (code & 0xFF) in _CORRUPTION_CODES


def _compressible(media_type: str) -> bool:
    return (
        media_type == "application/json"
        or media_type == "image/svg+xml"
        or media_type.startswith("text/")
    )


def _encode(data: bytes, media_type: str) -> tuple[bytes, str]:
    if len(data) >= _COMPRESS_MIN and _compressible(media_type):
        return zlib.compress(data, 6), "zlib"
    return data, "raw"


def _decode(blob: bytes, encoding: str) -> bytes:
    if encoding == "raw":
        return bytes(blob)
    if encoding == "zlib":
        try:
            return zlib.decompress(blob)
        except zlib.error:
            raise DamagedEntry("stored bytes do not decompress") from None
    raise DamagedEntry("unknown stored encoding")


class _SqliteCore:
    """连接池、PRAGMA、版本门、quick_check 自愈、事务作用域与写者租约:两种 db 共用。"""

    def __init__(
        self,
        db_path: Path,
        schema: str,
        *,
        synchronous: str,
        writer_scope: WriterScope = "process",
    ) -> None:
        if synchronous not in {"FULL", "NORMAL"}:
            raise ValueError("object_store_synchronous must be FULL or NORMAL")
        self.db_path = db_path
        self._schema = schema
        self._synchronous = synchronous
        self._writer_scope = writer_scope
        self._connections: dict[int, sqlite3.Connection] = {}
        self._lock = Lock()
        self._initialized = False
        self._writer_acquired = False
        self._closed = False
        self._txn_depth: ContextVar[int] = ContextVar(f"object_backend_txn_{id(self)}", default=0)

    # ---- 连接与初始化 -----------------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("backend is closed")
        ident = threading.get_ident()
        connection = self._connections.get(ident)
        if connection is not None:
            return connection
        with self._lock:
            if not self._initialized:
                self._initialize()
                self._initialized = True
        connection = self._connect()
        with self._lock:
            self._connections[ident] = connection
        return connection

    def reader_connection(self) -> sqlite3.Connection | None:
        """只读路径的连接:db 文件还不存在时返回 ``None``,**不**创建它。

        扫描 / 挂载一个纯文件布局(或旧代)的 store 根绝不能在那里留下一个空 db;
        只有第一次写(``connection()``)才建库。"""
        if self._closed:
            raise RuntimeError("backend is closed")
        if not self._initialized and not self.db_path.is_file():
            return None
        return self.connection()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False 只为 close() 能从关闭线程统一收尾:
        # 使用始终是线程本地的(connection() 按 thread id 取),从不跨线程共享游标。
        # 新库第一次切 WAL(``journal_mode``)撞上别的连接时直接回 SQLITE_BUSY、不走
        # busy handler:与 busy_timeout 同长的退避重试,而不是失败(更不是当成损坏)。
        deadline = monotonic() + _BUSY_SECONDS
        pause = 0.002
        while True:
            connection = sqlite3.connect(
                self.db_path, isolation_level=None, timeout=_BUSY_SECONDS, check_same_thread=False
            )
            try:
                self._apply_pragmas(connection)
            except sqlite3.OperationalError as error:
                connection.close()
                if not _busy(error) or monotonic() >= deadline:
                    raise
                sleep(pause)
                pause = min(pause * 2, 0.05)
                continue
            except sqlite3.Error:
                connection.close()
                raise
            return connection

    def _apply_pragmas(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA synchronous = {self._synchronous}")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA mmap_size = 0")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA wal_autocheckpoint = 2000")

    def _initialize(self) -> None:
        """首次使用:版本门 + 建表 + ``quick_check``;损坏 db 改名重建(一次)。

        只有"损坏"才重建(``SQLITE_CORRUPT`` / ``SQLITE_NOTADB`` / quick_check 不过);
        ``database is locked`` 一类 ``OperationalError`` 原样上抛——把别的进程正在写的 db
        当成损坏改名,会让那个进程之后的写全部落进改了名的旧文件(丢数据)。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open_checked()
        except sqlite3.DatabaseError as error:
            if not _corrupt(error):
                raise
            with self._init_guard():
                # 拿到写者互斥后再看一次:别的进程可能刚重建过,别把它的新库也改名。
                try:
                    self._open_checked(guarded=True)
                    return
                except sqlite3.DatabaseError as again:
                    if not _corrupt(again):
                        raise
                self._rebuild_corrupt()
                connection = self._connect()
                try:
                    self._gate_and_create(connection, guarded=True)
                finally:
                    connection.close()

    def _init_guard(self) -> AbstractContextManager[None]:
        """打开时要写(建表 / 重建)才取的写者互斥:按事务持租约的 db 取同一把短租约;
        按进程持租约的 db 照旧(租约在第一次写事务时才取)。"""
        if self._writer_scope == "transaction":
            return self._transaction_writer()
        return nullcontext()

    def _open_checked(self, *, guarded: bool = False) -> None:
        """版本门 + 必要时建表 + ``quick_check``;损坏以 ``sqlite3.DatabaseError`` 上抛。"""
        connection = self._connect()
        try:
            self._gate_and_create(connection, guarded=guarded)
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if check is None or check[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
        finally:
            connection.close()

    def _gate_and_create(self, connection: sqlite3.Connection, *, guarded: bool = False) -> None:
        # 版本门的几次读放进同一个读事务(同一快照):分开自动提交时,别的实例恰好在两次读
        # 之间提交建表,会读到"没打标却有表"而被误判成不是我们的 db。只读事务不写任何东西。
        connection.execute("BEGIN")
        try:
            current = self._gate(connection)
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        if current:
            return  # 本版本的表已经建好:打开一个现成的 db 不开写事务
        with nullcontext() if guarded else self._init_guard():
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 拿到写锁后在同一快照里再判一次:别的实例可能刚建好(或改了)这个 db。
                if not self._gate(connection):
                    for statement in self._schema.strip().split(";\n"):
                        text = statement.strip()
                        if text:
                            connection.execute(text)
                    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version = {USER_VERSION}")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    @staticmethod
    def _gate(connection: sqlite3.Connection) -> bool:
        """版本门(须在一个事务内调用):不是本代码可写的版本 → ``BackendSchemaError``;
        返回"已是本版本"(True)还是"还要建表 / 升级"(False)。"""
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id not in (0, APPLICATION_ID):
            raise BackendSchemaError("backend_schema_foreign_application_id")
        if user_version > USER_VERSION:
            raise BackendSchemaError("backend_schema_newer_user_version")
        if application_id == 0:
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            if int(tables) > 0:
                # 有表却没打我们的标:不是我们的 db,拒绝(版本门)。
                raise BackendSchemaError("backend_schema_unmarked_database")
            return False
        return user_version == USER_VERSION

    def _rebuild_corrupt(self) -> None:
        """quick_check / 打开失败的 db:改名 ``.corrupt-<utc>``,从零重建;计一次修复。"""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.db_path) + suffix)
            if source.exists():
                os.replace(source, Path(f"{source}.corrupt-{stamp}"))
        note_repair("store_db")

    # ---- 事务作用域(可重入,contextvar)----------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._writer_scope == "transaction" and self._txn_depth.get() == 0:
            # 先在租约之外拿到本线程的连接:首次打开要写时自己会取同一把(不可重入的)租约。
            self.connection()
            with self._transaction_writer(), self._scoped_transaction():
                yield
            return
        if self._writer_scope == "process":
            self._acquire_writer()
        with self._scoped_transaction():
            yield

    @contextmanager
    def _scoped_transaction(self) -> Iterator[None]:
        depth = self._txn_depth.get()
        connection = self.connection()
        if depth == 0:
            connection.execute("BEGIN IMMEDIATE")
        token = self._txn_depth.set(depth + 1)
        try:
            yield
        except BaseException:
            if depth == 0:
                connection.execute("ROLLBACK")
            raise
        else:
            if depth == 0:
                connection.execute("COMMIT")
        finally:
            self._txn_depth.reset(token)

    # ---- 写者租约(跨进程互斥;同进程可重入)------------------------------------------

    def _lease_base(self) -> Path:
        return Path(str(self.db_path) + ".writer")

    @contextmanager
    def _transaction_writer(self) -> Iterator[None]:
        """按事务的写者互斥:同进程排队于按 db 路径的锁,跨进程排队于非持久的
        ``<db>.writer`` 短租约(等至多 ``TRANSACTION_LEASE_WAIT_SECONDS``,仍拿不到 →
        ``StoreBusy``);事务结束即释放,别的进程随后就能写(根级共享模型缓存)。"""
        base = self._lease_base()
        with _transaction_lock(self.db_path):
            owner = lease.current_owner(WRITER_CLAIM_FORMAT, TRANSACTION_LEASE_SECONDS)
            content = lease.owner_payload(WRITER_CLAIM_FORMAT, owner)
            deadline = monotonic() + TRANSACTION_LEASE_WAIT_SECONDS
            pause = 0.002
            while (
                lease.acquire_lease(base, content, claim_format=WRITER_CLAIM_FORMAT, durable=False)
                is None
            ):
                if monotonic() >= deadline:
                    raise StoreBusy("store_busy")
                sleep(pause)
                pause = min(pause * 2, 0.05)
            try:
                yield
            finally:
                lease.release_lease(base)

    def _acquire_writer(self) -> None:
        base = self._lease_base()
        with _WRITER_LOCK:
            # 锁内判断:同一实例的几个线程(文档内页级并发,ADR 0045)可能同时首写,只记一次。
            if self._writer_acquired:
                return
            count = _WRITER_COUNTS.get(self.db_path, 0)
            if count == 0:
                owner = lease.current_owner(WRITER_CLAIM_FORMAT, WRITER_LEASE_SECONDS)
                content = lease.owner_payload(WRITER_CLAIM_FORMAT, owner)
                if lease.holder_process(base) != lease.PROCESS_TOKEN:
                    generation = lease.acquire_lease(
                        base, content, claim_format=WRITER_CLAIM_FORMAT
                    )
                    if generation is None:
                        raise StoreBusy("store_busy")
            _WRITER_COUNTS[self.db_path] = count + 1
            self._writer_acquired = True

    # ---- 关闭 --------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            with suppress(sqlite3.Error):
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
        if self._writer_acquired:
            with _WRITER_LOCK:
                self._writer_acquired = False
                remaining = _WRITER_COUNTS.get(self.db_path, 1) - 1
                if remaining <= 0:
                    _WRITER_COUNTS.pop(self.db_path, None)
                    lease.release_lease(self._lease_base())
                else:
                    _WRITER_COUNTS[self.db_path] = remaining


class SqliteBackend:
    """一个 store 根目录的 sqlite 后端;``FileBackend`` 兼作只读 legacy 层与外置对象层。"""

    kind: BackendKind = "sqlite"

    def __init__(
        self,
        root: Path,
        *,
        db_name: str = "store.sqlite",
        db_path: Path | None = None,
        synchronous: str = "FULL",
        inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
        external_media_types: frozenset[str] = frozenset({"application/pdf"}),
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        media_inline_max_bytes: Mapping[str, int] | None = None,
        core: _SqliteCore | None = None,
        scope: str | None = None,
    ) -> None:
        self.root = root
        self._files = FileBackend(root)
        # db_path:db 放在别处(StagedBackend 的本地工作副本);缺省 = <root>/<db_name>。
        # core + scope:文档 db 里的一个 scope(ADR 0047),连接 / 事务 / 写者租约与同文档的
        # 其他 scope 共用(调用方拥有 core,本实例不关它)。
        db_file = root / db_name if db_path is None else db_path
        self._core = (
            _SqliteCore(db_file, STORE_SCHEMA, synchronous=synchronous) if core is None else core
        )
        self._tables = _Tables() if scope is None else _Tables.scoped(scope)
        self._inline_max_bytes = inline_max_bytes
        self._external_media_types = external_media_types
        self._max_db_bytes = max_db_bytes
        # 按媒体类型的内联上限(文档 db 的 PDF 例外);缺省没有例外。
        self._media_inline_max_bytes = dict(media_inline_max_bytes or {})

    # ---- 内容寻址对象 ------------------------------------------------------------------

    def _external(self, data: bytes, media_type: str) -> bool:
        limit = self._media_inline_max_bytes.get(media_type, self._inline_max_bytes)
        if media_type in self._external_media_types or len(data) > limit:
            return True
        if len(data) <= _OVERFLOW_INLINE_MAX:
            return False
        try:
            return self._core.db_path.stat().st_size > self._max_db_bytes
        except OSError:
            return False

    def _object_row(self, digest: str) -> tuple[int, str, int, bytes | None] | None:
        connection = self._core.reader_connection()
        if connection is None:
            return None
        row = connection.execute(
            "SELECT byte_length, encoding, external, bytes"
            f" FROM {self._tables.objects} WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1]), int(row[2]), row[3]

    def get_object(self, digest: str) -> bytes | None:
        _require_digest(digest)
        row = self._object_row(digest)
        if row is None:
            return self._files.get_object(digest)
        byte_length, encoding, external, blob = row
        if external:
            # 索引行只是提示,不是字节:文件丢了 → 与文件布局同义的"缺失"(None),
            # 文件被改 → ``DamagedEntry``;两者都由下一次携带字节的写修复(ADR 0029)。
            return self._files.get_object(digest)
        if blob is None:
            raise DamagedEntry("Stored artifact digest mismatch; source review is unavailable")
        data = _decode(blob, encoding)
        if len(data) != byte_length or hashlib.sha256(data).hexdigest() != digest:
            raise DamagedEntry("Stored artifact digest mismatch; source review is unavailable")
        return data

    def get_content(self, digest: str) -> bytes | None:
        """db 的完整读顺序(设计稿 §5):``objects`` 行 → ``stage_cache.product``
        (按 ``artifact_digest`` 索引)→ 文件布局(含旧代内联指针的扫描)。"""
        _require_digest(digest)
        data = self.get_object(digest)  # 与文件布局一致:损坏的对象条目在读时即拒绝
        if data is not None:
            return data
        data = self._product_row(digest)
        if data is not None:
            return data
        return self._files.get_content(digest)

    def _product_row(self, digest: str) -> bytes | None:
        """``stage_cache`` 里内联携带 ``digest`` 产物的行;损坏的行当作缺失(写路径修复)。"""
        connection = self._core.reader_connection()
        if connection is None:
            return None
        rows = connection.execute(
            f"SELECT product, product_encoding FROM {self._tables.stage_cache}"
            " WHERE artifact_digest = ? AND product IS NOT NULL",
            (digest,),
        ).fetchall()
        for blob, encoding in rows:
            try:
                data = _decode(bytes(blob), str(encoding))
            except DamagedEntry:
                continue
            if hashlib.sha256(data).hexdigest() == digest:
                return data
        return None

    def note_product(self, digest: str, fingerprint: str) -> None:
        # db 行有自己的 artifact_digest 索引;旧代内联指针仍喂文件层的进程内索引。
        self._files.note_product(digest, fingerprint)

    def object_location(self, digest: str) -> Path | None:
        _require_digest(digest)
        row = self._object_row(digest)
        if row is None:
            if self._product_row(digest) is not None:
                return None  # 内联在 db 的 stage 行里
            return self._files.object_location(digest)
        if row[2]:  # external
            return self._files.object_location(digest)
        return None

    def content_path(self, digest: str) -> Path:
        """外置与旧代文件给其路径;住在 db 行里(或不存在,将写进 db)→ ``LookupError``。"""
        _require_digest(digest)
        located = self.object_location(digest)
        if located is None:
            raise LookupError("Object lives in the store database; there is no file to watch")
        return located

    def read_existing(self, digest: str) -> bytes | None:
        _require_digest(digest)
        connection = self._core.reader_connection()
        row = (
            None
            if connection is None
            else connection.execute(
                f"SELECT encoding, external, bytes FROM {self._tables.objects} WHERE digest = ?",
                (digest,),
            ).fetchone()
        )
        if row is None:
            return self._files.read_existing(digest)
        encoding, external, blob = row
        if external:
            return self._files.read_existing(digest)
        if blob is None:
            return None
        try:
            return _decode(blob, encoding)
        except DamagedEntry:
            return bytes(blob)

    def put_object(
        self, digest: str, data: bytes, media_type: str, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        del replace
        _require_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Object bytes do not hash to their digest")
        if self._external(data, media_type):
            return self._put_external(digest, data, media_type)
        blob, encoding = _encode(data, media_type)
        with self._core.transaction():
            connection = self._core.connection()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO objects"
                " (digest, byte_length, media_type, encoding, external, bytes, created_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?)",
                (digest, len(data), media_type, encoding, blob, time()),
            )
            joined = self._join_scope(digest)
            if cursor.rowcount == 1:
                return "placed"
            if self._row_intact(digest, data):
                return "placed" if joined else "existing"
            connection.execute(
                "UPDATE objects SET byte_length = ?, media_type = ?, encoding = ?,"
                " external = 0, bytes = ? WHERE digest = ?",
                (len(data), media_type, encoding, blob, digest),
            )
            note_repair("object")
            return "placed"

    def _put_external(
        self, digest: str, data: bytes, media_type: str
    ) -> Literal["placed", "existing"]:
        with self._core.transaction():
            connection = self._core.connection()
            result = self._files.put_object(digest, data, media_type)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO objects"
                " (digest, byte_length, media_type, encoding, external, bytes, created_at)"
                " VALUES (?, ?, ?, 'raw', 1, NULL, ?)",
                (digest, len(data), media_type, time()),
            )
            joined = self._join_scope(digest)
            if cursor.rowcount == 1:
                return result
            row = connection.execute(
                "SELECT external FROM objects WHERE digest = ?", (digest,)
            ).fetchone()
            if row is not None and not row[0] and not self._row_intact(digest, data):
                # 既有内联行已损坏:这份字节现已外置,索引行改指外置(ADR 0029 修复)。
                connection.execute(
                    "UPDATE objects SET byte_length = ?, media_type = ?, encoding = 'raw',"
                    " external = 1, bytes = NULL WHERE digest = ?",
                    (len(data), media_type, digest),
                )
                note_repair("object")
                return "placed"
            return "placed" if joined else result

    def _join_scope(self, digest: str) -> bool:
        """文档 db 的 scope:记一条成员行(字节已在共用的 ``objects`` 里);新成员 → ``True``。
        须在写事务内调用;每个 store 根一个 db 时什么都不做。"""
        if self._tables.refs is None:
            return False
        cursor = self._core.connection().execute(
            f"INSERT OR IGNORE INTO {self._tables.refs} (digest) VALUES (?)", (digest,)
        )
        return cursor.rowcount == 1

    def _row_intact(self, digest: str, data: bytes) -> bool:
        """``INSERT OR IGNORE`` 被忽略后的读回校验:行里就是这些字节吗?"""
        row = (
            self._core.connection()
            .execute(
                "SELECT byte_length, encoding, external, bytes FROM objects WHERE digest = ?",
                (digest,),
            )
            .fetchone()
        )
        if row is None:
            return False
        byte_length, encoding, external, blob = row
        if external:
            existing = self._files.read_existing(digest)
            return existing == data
        if blob is None or byte_length != len(data):
            return False
        try:
            return _decode(blob, encoding) == data
        except DamagedEntry:
            return False

    def object_names(self) -> list[str]:
        connection = self._core.reader_connection()
        rows = (
            []
            if connection is None
            else connection.execute(f"SELECT digest FROM {self._tables.objects}").fetchall()
        )
        names = {str(row[0]) for row in rows}
        names.update(self._files.object_names())
        return sorted(names)

    # ---- stage-cache 条目 --------------------------------------------------------------

    def stage_entry(self, fingerprint: str) -> StageEntry | None:
        _require_digest(fingerprint)
        connection = self._core.reader_connection()
        row = (
            None
            if connection is None
            else connection.execute(
                "SELECT envelope_digest, envelope, product, product_encoding"
                f" FROM {self._tables.stage_cache} WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        )
        if row is None:
            return self._files.stage_entry(fingerprint)
        envelope_digest, envelope_blob, product_blob, product_encoding = row
        envelope = bytes(envelope_blob)
        if hashlib.sha256(envelope).hexdigest() != envelope_digest:
            raise DamagedEntry("damaged pointer")
        if product_blob is None:
            return StageEntry(str(envelope_digest), envelope)
        product = _decode(product_blob, product_encoding)
        if hashlib.sha256(product).hexdigest() != _artifact_digest(envelope):
            raise DamagedEntry("damaged pointer")
        return StageEntry(str(envelope_digest), envelope, product)

    def put_stage_entry(
        self, fingerprint: str, entry: StageEntry, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        _require_digest(fingerprint)
        if hashlib.sha256(entry.envelope).hexdigest() != entry.envelope_digest:
            raise ValueError("Stage envelope does not hash to its digest line")
        stage, producer, artifact_digest = _envelope_columns(entry.envelope)
        product_blob: bytes | None = None
        product_encoding = "raw"
        if entry.product is not None:
            if hashlib.sha256(entry.product).hexdigest() != artifact_digest:
                raise ValueError("Inline stage product does not hash to its artifact digest")
            product_blob, product_encoding = _encode(entry.product, "application/json")
        row_values = (
            fingerprint,
            entry.envelope_digest,
            entry.envelope,
            stage,
            producer,
            artifact_digest,
            product_blob,
            product_encoding,
            time(),
        )
        with self._core.transaction():
            connection = self._core.connection()
            if replace:
                connection.execute(
                    f"INSERT OR REPLACE INTO {self._tables.stage_cache} (fingerprint,"
                    " envelope_digest, envelope,"
                    " stage, producer, artifact_digest, product, product_encoding, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row_values,
                )
                return "placed"
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO {self._tables.stage_cache} (fingerprint, envelope_digest,"
                " envelope,"
                " stage, producer, artifact_digest, product, product_encoding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row_values,
            )
            if cursor.rowcount == 1:
                return "placed"
            existing = connection.execute(
                f"SELECT envelope_digest, envelope FROM {self._tables.stage_cache}"
                " WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                held_digest, held_blob = str(existing[0]), bytes(existing[1])
                if hashlib.sha256(held_blob).hexdigest() == held_digest:
                    if held_digest == entry.envelope_digest:
                        return "existing"
                    raise StoreConflict("Conflicting immutable stage cache entry")
            # 行已损坏(信封不是其摘要):以这份完好条目替换(ADR 0029 的自愈)。
            connection.execute(
                f"INSERT OR REPLACE INTO {self._tables.stage_cache} (fingerprint, envelope_digest,"
                " envelope,"
                " stage, producer, artifact_digest, product, product_encoding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row_values,
            )
            note_repair("stage_cache")
            return "placed"

    # ---- 命名指针与可变记录 ------------------------------------------------------------

    def pointer(self, name: str) -> str | None:
        connection = self._core.reader_connection()
        row = (
            None
            if connection is None
            else connection.execute(
                f"SELECT digest FROM {self._tables.pointers} WHERE name = ?", (name,)
            ).fetchone()
        )
        if row is not None:
            digest = str(row[0])
            return digest if _DIGEST.fullmatch(digest) else None
        return self._files.pointer(name)

    def set_pointer(self, name: str, digest: str) -> None:
        _require_digest(digest)
        with self._core.transaction():
            self._core.connection().execute(
                f"INSERT OR REPLACE INTO {self._tables.pointers} (name, digest, updated_at)"
                " VALUES (?, ?, ?)",
                (name, digest, time()),
            )

    def record(self, name: str) -> bytes | None:
        connection = self._core.reader_connection()
        row = (
            None
            if connection is None
            else connection.execute(
                f"SELECT bytes FROM {self._tables.records} WHERE name = ?", (name,)
            ).fetchone()
        )
        if row is not None:
            return bytes(row[0])
        return self._files.record(name)

    def put_record(self, name: str, data: bytes) -> None:
        with self._core.transaction():
            self._core.connection().execute(
                f"INSERT OR REPLACE INTO {self._tables.records} (name, bytes, updated_at)"
                " VALUES (?, ?, ?)",
                (name, data, time()),
            )

    # ---- pin / 事务 / 批量校验 ----------------------------------------------------------

    def _db_marks(self) -> tuple[int, ...]:
        connection = self._core.reader_connection()
        data_version = (
            0
            if connection is None
            else int(connection.execute("PRAGMA data_version").fetchone()[0])
        )
        marks = [data_version]
        for suffix in ("", "-wal"):
            try:
                status = os.stat(str(self._core.db_path) + suffix)
                marks.extend((status.st_size, status.st_mtime_ns))
            except OSError:
                marks.extend((0, 0))
        return tuple(marks)

    def pin(self, digest: str) -> PinToken:
        _require_digest(digest)
        row = self._object_row(digest)
        if row is None or row[2]:
            return self._files.pin(digest)
        return PinToken(digest, "sqlite", self._db_marks())

    def pin_unchanged(self, token: PinToken) -> bool:
        if token.backend == "files":
            return self._files.pin_unchanged(token)
        if self._db_marks() == token.marks:
            return True
        try:
            return self.get_object(token.digest) is not None
        except DamagedEntry:
            return False

    def transaction(self) -> AbstractContextManager[None]:
        return self._core.transaction()

    def verify_many(self, digests: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """流式批量读回并校验:每批 ≤ 500 个摘要一条 ``IN`` 查询读 db 行,db 里没有的
        (外置 / 旧代 / 内联产物)逐个走完整读顺序;损坏 → ``DamagedEntry``。"""
        batch: list[str] = []
        for digest in digests:
            _require_digest(digest)
            batch.append(digest)
            if len(batch) >= _VERIFY_BATCH:
                yield from self._verify_batch(batch)
                batch = []
        if batch:
            yield from self._verify_batch(batch)

    def _verify_batch(self, batch: list[str]) -> Iterator[tuple[str, bytes]]:
        connection = self._core.reader_connection()
        rows: dict[str, tuple[int, str, int, bytes | None]] = {}
        if connection is not None:
            marks = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT digest, byte_length, encoding, external, bytes"
                f" FROM {self._tables.objects} WHERE digest IN ({marks})",
                batch,
            ):
                rows[str(row[0])] = (int(row[1]), str(row[2]), int(row[3]), row[4])
        for digest in batch:
            row = rows.get(digest)
            if row is None or row[2]:
                data = self.get_content(digest)
                if data is None:
                    raise LookupError("Object to verify is absent")
                yield digest, data
                continue
            byte_length, encoding, _, blob = row
            if blob is None:
                raise DamagedEntry("Stored artifact digest mismatch; source review is unavailable")
            data = _decode(blob, encoding)
            if len(data) != byte_length or hashlib.sha256(data).hexdigest() != digest:
                raise DamagedEntry("Stored artifact digest mismatch; source review is unavailable")
            yield digest, data

    def close(self) -> None:
        self._core.close()


def _envelope_columns(envelope: bytes) -> tuple[str, str, str]:
    """信封 JSON 里的 stage / producer / artifact 摘要(供索引列;容错,缺失给空串)。"""
    try:
        outcome = json.loads(envelope).get("outcome", {})
    except (ValueError, AttributeError):
        outcome = {}
    if not isinstance(outcome, dict):
        outcome = {}
    stage = outcome.get("stage")
    producer = outcome.get("producer")
    artifact = outcome.get("artifact")
    digest = artifact.get("sha256") if isinstance(artifact, dict) else None
    return (
        stage if isinstance(stage, str) else "",
        producer if isinstance(producer, str) else "",
        digest if isinstance(digest, str) else "",
    )


class SqliteModelCacheBackend:
    """模型缓存的 sqlite 后端:requests / responses / contexts / claims 四表 +
    文件布局只读回退;旧 ``.claim`` 文件仍按 ADR 0023 的 mtime 规则被尊重。

    事务按调用提交(claim 建立 / 续租 / 记录写入 / 响应 / 上下文 / 释放各一事务),
    写者租约按事务持有:根级共享缓存被多个进程轮流写时互不长期占用。
    读顺序 db 行 → 旧文件(``requests/`` / ``responses/`` / ``contexts/``);旧文件
    只读,完好的不改写不搬迁,损坏的由 db 行替代。一个实例第一次用到某个旧目录时
    探测它在不在,之后不再探测(旧文件只由 PR-3 之前的代码写出;此后才出现的旧目录由
    下一个实例看到)。"""

    kind: BackendKind = "sqlite"

    def __init__(
        self,
        cache_dir: Path,
        *,
        db_name: str = "model-cache.sqlite",
        db_path: Path | None = None,
        synchronous: str = "FULL",
        core: _SqliteCore | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self._files = FileModelCacheBackend(cache_dir)
        # db_path:db 放在别处(StagedModelCacheBackend 的本地工作副本);缺省 = <cache_dir>/<db_name>。
        # core:文档 db(ADR 0047)的共用连接 / 事务 / 按进程的写者租约(调用方拥有)。
        self._core = (
            _SqliteCore(
                cache_dir / db_name if db_path is None else db_path,
                MODEL_CACHE_SCHEMA,
                synchronous=synchronous,
                writer_scope="transaction",
            )
            if core is None
            else core
        )
        self._legacy_dirs: dict[str, bool] = {}

    def _legacy(self, directory: str) -> bool:
        """旧布局目录 ``cache_dir/<directory>`` 在不在(每个实例只探测一次)。"""
        present = self._legacy_dirs.get(directory)
        if present is None:
            present = (self.cache_dir / directory).is_dir()
            self._legacy_dirs[directory] = present
        return present

    # ---- 记录 / 响应 / 上下文 ----------------------------------------------------------

    def record(self, key: str) -> bytes | None:
        row = (
            self._core.connection()
            .execute("SELECT record FROM requests WHERE record_key = ?", (key,))
            .fetchone()
        )
        if row is not None:
            return bytes(row[0])
        return self._files.record(key) if self._legacy("requests") else None

    def put_record(self, key: str, data: bytes, *, replace_damaged: bool = False) -> None:
        columns = _record_columns(key, data)
        with self._core.transaction():
            connection = self._core.connection()
            if replace_damaged:
                connection.execute(_REQUESTS_REPLACE, columns)
                return
            cursor = connection.execute(_REQUESTS_INSERT, columns)
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT record FROM requests WHERE record_key = ?", (key,)
            ).fetchone()
            if row is not None and bytes(row[0]) == data:
                return
            raise StoreConflict("cache_conflict")

    def has_records(self) -> bool:
        row = self._core.connection().execute("SELECT 1 FROM requests LIMIT 1").fetchone()
        return row is not None or (self._legacy("requests") and self._files.has_records())

    def response(self, digest: str) -> bytes | None:
        _require_digest(digest)
        row = (
            self._core.connection()
            .execute("SELECT encoding, bytes FROM responses WHERE digest = ?", (digest,))
            .fetchone()
        )
        if row is None:
            return self._files.response(digest) if self._legacy("responses") else None
        data = _decode(bytes(row[1]), str(row[0]))
        if hashlib.sha256(data).hexdigest() != digest:
            raise DamagedEntry("cached_response_digest_mismatch")
        return data

    def put_response(self, digest: str, data: bytes) -> None:
        _require_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Response bytes do not hash to their digest")
        blob, encoding = _encode(data, "application/json")
        with self._core.transaction():
            connection = self._core.connection()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO responses (digest, byte_length, encoding, bytes,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (digest, len(data), encoding, blob, time()),
            )
            if cursor.rowcount == 1:
                return
            row = connection.execute(
                "SELECT encoding, bytes FROM responses WHERE digest = ?", (digest,)
            ).fetchone()
            try:
                intact = row is not None and _decode(bytes(row[1]), str(row[0])) == data
            except DamagedEntry:
                intact = False
            if not intact:
                # 内容寻址:同名异字节是损坏,直接替换(ADR 0029)。
                connection.execute(
                    "UPDATE responses SET byte_length = ?, encoding = ?, bytes = ?"
                    " WHERE digest = ?",
                    (len(data), encoding, blob, digest),
                )

    def context(self, fingerprint: str) -> bytes | None:
        _require_digest(fingerprint)
        row = (
            self._core.connection()
            .execute(
                "SELECT encoding, bytes FROM contexts WHERE request_fingerprint = ?",
                (fingerprint,),
            )
            .fetchone()
        )
        if row is None:
            return self._files.context(fingerprint) if self._legacy("contexts") else None
        return _decode(bytes(row[1]), str(row[0]))

    def put_context(self, fingerprint: str, data: bytes) -> None:
        _require_digest(fingerprint)
        # 已有(db 行或旧文件)即 no-op,且不开写事务:回放一轮只读不写。
        if (
            self._core.connection()
            .execute("SELECT 1 FROM contexts WHERE request_fingerprint = ?", (fingerprint,))
            .fetchone()
            is not None
        ):
            return
        if (
            self._legacy("contexts")
            and (self.cache_dir / "contexts" / f"{fingerprint}.json").exists()
        ):
            return
        blob, encoding = _encode(data, "application/json")
        with self._core.transaction():
            connection = self._core.connection()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO contexts (request_fingerprint, encoding, bytes,"
                " created_at) VALUES (?, ?, ?, ?)",
                (fingerprint, encoding, blob, time()),
            )
            # 首个存进去的上下文胜出,后来者静默让位(与 json_completion._store_context
            # 的 ``path.exists() -> return`` 一致;差异上报是 PR-3 调用方的事)。
            del cursor

    # ---- claim / renew / release(ADR 0023 的 claims 表)-------------------------------

    def claim(
        self,
        key: str,
        owner: ClaimOwner,
        *,
        expired: lease.Expired | None = None,
    ) -> int | None:
        """读顺序 claims 行 → 旧 ``.claim`` 文件。判定在事务外(每个调用方对当前持有者
        只判一次),取得在事务内以比较并交换完成——与文件布局的 O_EXCL 创建同义:
        无行 → ``INSERT OR IGNORE``(被忽略 = 别人刚拿到);有行且已过期 →
        ``UPDATE … WHERE generation = 读到的代次``(``changes() == 1`` 才算接管)。
        旧文件的持有者已结束时以其代次 + 1 接管(与文件布局的接管代次一致)。"""
        judge = expired if expired is not None else _default_expired(owner)
        columns = _claim_columns(key, owner)
        row = (
            self._core.connection()
            .execute(
                "SELECT generation, claim, created_at FROM claims WHERE record_key = ?", (key,)
            )
            .fetchone()
        )
        if row is None:
            generation = 0
            legacy = self._legacy_holder(key)
            if legacy is not None:
                held_generation, held, modified = legacy
                if not judge(held, modified):
                    return None
                generation = held_generation + 1
            with self._core.transaction():
                cursor = self._core.connection().execute(
                    "INSERT OR IGNORE INTO claims (record_key, generation, claim, host, pid,"
                    " process, created_at, lease_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, generation, *columns),
                )
                return generation if cursor.rowcount == 1 else None
        generation, held_claim, held_created = int(row[0]), str(row[1]), float(row[2])
        if not judge(held_claim.encode(), held_created):
            return None
        return self._advance(key, generation, columns)

    def renew(self, key: str, owner: ClaimOwner, generation: int) -> int | None:
        """重试前续租:仍是第 ``generation`` 代的持有者才前进一代(ADR 0035)。"""
        return self._advance(key, generation, _claim_columns(key, owner))

    def _advance(self, key: str, generation: int, columns: _ClaimColumns) -> int | None:
        with self._core.transaction():
            cursor = self._core.connection().execute(
                "UPDATE claims SET generation = ?, claim = ?, host = ?, pid = ?, process = ?,"
                " created_at = ?, lease_seconds = ? WHERE record_key = ? AND generation = ?",
                (generation + 1, *columns, key, generation),
            )
            return generation + 1 if cursor.rowcount == 1 else None

    def claimed(self, key: str) -> bool:
        row = (
            self._core.connection()
            .execute("SELECT 1 FROM claims WHERE record_key = ?", (key,))
            .fetchone()
        )
        return row is not None or (self._legacy("requests") and self._files.claimed(key))

    def _legacy_holder(self, key: str) -> tuple[int, bytes, float] | None:
        """旧代码留下的 ``.claim`` 文件里最高代次的持有者:(代次, 字节, mtime)。"""
        if not self._legacy("requests"):
            return None
        base = self.cache_dir / "requests" / f"{key}.json.claim"
        generation = lease.latest_generation(base)
        holder = lease.generation_path(base, generation)
        try:
            return generation, holder.read_bytes(), holder.stat().st_mtime
        except OSError:
            return None

    def release(self, key: str, owner: ClaimOwner) -> None:
        """删本进程持有的 claims 行;本进程接管过的旧 ``.claim`` 文件一并删掉(与文件布局
        "记录写好即整组释放"一致;只有持有该请求的调用方才会走到这里)。"""
        with self._core.transaction():
            self._core.connection().execute(
                "DELETE FROM claims WHERE record_key = ? AND process = ?",
                (key, owner.process),
            )
        if self._legacy("requests"):
            self._files.release(key, owner)

    def transaction(self) -> AbstractContextManager[None]:
        return self._core.transaction()

    def close(self) -> None:
        self._core.close()


def _default_expired(owner: ClaimOwner) -> lease.Expired:
    def expired(content: bytes, modified: float) -> bool:
        return lease.lease_expired(
            content, modified, claim_format=CLAIM_FORMAT, process_token=owner.process
        )

    return expired


def _claim_columns(key: str, owner: ClaimOwner) -> _ClaimColumns:
    """claims 行的 (claim, host, pid, process, created_at, lease_seconds);claim 列与
    文件布局的 ``.claim`` 字节相同(ADR 0023 的持有者 JSON,从不含正文)。"""
    payload = lease.owner_payload(
        CLAIM_FORMAT, owner, extra={"request_fingerprint": key.partition(".")[0]}
    ).decode()
    return payload, owner.host, owner.pid, owner.process, owner.created_at, owner.lease_seconds


_REQUESTS_INSERT = (
    "INSERT OR IGNORE INTO requests (record_key, request_fingerprint, record, response_digest,"
    " failure_code, provider_error_param, provider_error_code, claim_takeover, attempt,"
    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_REQUESTS_REPLACE = _REQUESTS_INSERT.replace("INSERT OR IGNORE", "INSERT OR REPLACE")


def _record_columns(
    key: str, data: bytes
) -> tuple[str, str, bytes, str | None, str | None, str | None, str | None, int | None, int, float]:
    """记录 JSON 的索引列(容错解析;记录字节本身原样入 ``record`` 列)。"""
    try:
        document = json.loads(data)
    except ValueError:
        document = {}
    if not isinstance(document, dict):
        document = {}
    diagnostics = document.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    fingerprint = document.get("request_fingerprint")
    response_digest = document.get("response_digest")
    failure_code = document.get("failure_code")
    param = diagnostics.get("provider_error_param")
    code = diagnostics.get("provider_error_code")
    takeover = diagnostics.get("claim_takeover")
    return (
        key,
        fingerprint if isinstance(fingerprint, str) else key.partition(".")[0],
        data,
        response_digest if isinstance(response_digest, str) else None,
        failure_code if isinstance(failure_code, str) else None,
        param if isinstance(param, str) else None,
        code if isinstance(code, str) else None,
        takeover if isinstance(takeover, int) else None,
        2 if key.endswith(".retry-1") else 1,
        time(),
    )
