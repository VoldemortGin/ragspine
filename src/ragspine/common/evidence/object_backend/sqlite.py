"""sqlite 对象库后端:每个 store 根一个 db,小对象内联、大对象外置、读穿旧文件布局。

设计稿 §2(schema 与 PRAGMA)、§3(等价映射)、§4(并发)、§5(兼容读顺序)的实现:

- 写一律进 db(``INSERT OR IGNORE`` + 读回校验 + 修复);读顺序 db → sharded 文件 →
  flat 文件(``FileBackend`` 作为只读 legacy 层组合进来,外置大对象也经它落盘);
- 同进程多线程:线程本地连接池;事务作用域是可重入的 contextvar;
- 多进程:不信任 FUSE 上的 sqlite 文件锁,写者互斥用 O_EXCL 的 ``<db>.writer``
  租约文件(``lease.py``,复用 ADR 0023 的持有者 JSON + 租约 + 接管代次),
  拿不到 → ``StoreBusy``;
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
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import time
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

# 本进程已持有的写者租约:db 路径 → 持有它的后端实例数(同进程可重入,见 §4)。
_WRITER_LOCK = Lock()
_WRITER_COUNTS: dict[Path, int] = {}


def _require_digest(digest: str) -> None:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("Invalid content-addressed artifact identifier")


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

    def __init__(self, db_path: Path, schema: str, *, synchronous: str) -> None:
        if synchronous not in {"FULL", "NORMAL"}:
            raise ValueError("object_store_synchronous must be FULL or NORMAL")
        self.db_path = db_path
        self._schema = schema
        self._synchronous = synchronous
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
        connection = sqlite3.connect(
            self.db_path, isolation_level=None, timeout=30.0, check_same_thread=False
        )
        try:
            self._apply_pragmas(connection)
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
        """首次使用:版本门 + 建表 + ``quick_check``;损坏 db 改名重建(一次)。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = self._connect()
            try:
                self._gate_and_create(connection)
                check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if check is None or check[0] != "ok":
                    raise sqlite3.DatabaseError("quick_check failed")
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            self._rebuild_corrupt()
            connection = self._connect()
            try:
                self._gate_and_create(connection)
            finally:
                connection.close()

    def _gate_and_create(self, connection: sqlite3.Connection) -> None:
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
        connection.execute("BEGIN IMMEDIATE")
        try:
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
        self._acquire_writer()
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

    def _acquire_writer(self) -> None:
        if self._writer_acquired:
            return
        base = self._lease_base()
        with _WRITER_LOCK:
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

    kind: Literal["files", "sqlite"] = "sqlite"

    def __init__(
        self,
        root: Path,
        *,
        db_name: str = "store.sqlite",
        synchronous: str = "FULL",
        inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES,
        external_media_types: frozenset[str] = frozenset({"application/pdf"}),
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    ) -> None:
        self.root = root
        self._files = FileBackend(root)
        self._core = _SqliteCore(root / db_name, STORE_SCHEMA, synchronous=synchronous)
        self._inline_max_bytes = inline_max_bytes
        self._external_media_types = external_media_types
        self._max_db_bytes = max_db_bytes

    # ---- 内容寻址对象 ------------------------------------------------------------------

    def _external(self, data: bytes, media_type: str) -> bool:
        if media_type in self._external_media_types or len(data) > self._inline_max_bytes:
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
            "SELECT byte_length, encoding, external, bytes FROM objects WHERE digest = ?",
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
            "SELECT product, product_encoding FROM stage_cache"
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
                "SELECT encoding, external, bytes FROM objects WHERE digest = ?", (digest,)
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
            if cursor.rowcount == 1:
                return "placed"
            if self._row_intact(digest, data):
                return "existing"
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
            return result

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
        rows = [] if connection is None else connection.execute("SELECT digest FROM objects").fetchall()
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
                " FROM stage_cache WHERE fingerprint = ?",
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
                    "INSERT OR REPLACE INTO stage_cache (fingerprint, envelope_digest, envelope,"
                    " stage, producer, artifact_digest, product, product_encoding, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row_values,
                )
                return "placed"
            cursor = connection.execute(
                "INSERT OR IGNORE INTO stage_cache (fingerprint, envelope_digest, envelope,"
                " stage, producer, artifact_digest, product, product_encoding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row_values,
            )
            if cursor.rowcount == 1:
                return "placed"
            existing = connection.execute(
                "SELECT envelope_digest, envelope FROM stage_cache WHERE fingerprint = ?",
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
                "INSERT OR REPLACE INTO stage_cache (fingerprint, envelope_digest, envelope,"
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
            else connection.execute("SELECT digest FROM pointers WHERE name = ?", (name,)).fetchone()
        )
        if row is not None:
            digest = str(row[0])
            return digest if _DIGEST.fullmatch(digest) else None
        return self._files.pointer(name)

    def set_pointer(self, name: str, digest: str) -> None:
        _require_digest(digest)
        with self._core.transaction():
            self._core.connection().execute(
                "INSERT OR REPLACE INTO pointers (name, digest, updated_at) VALUES (?, ?, ?)",
                (name, digest, time()),
            )

    def record(self, name: str) -> bytes | None:
        connection = self._core.reader_connection()
        row = (
            None
            if connection is None
            else connection.execute("SELECT bytes FROM records WHERE name = ?", (name,)).fetchone()
        )
        if row is not None:
            return bytes(row[0])
        return self._files.record(name)

    def put_record(self, name: str, data: bytes) -> None:
        with self._core.transaction():
            self._core.connection().execute(
                "INSERT OR REPLACE INTO records (name, bytes, updated_at) VALUES (?, ?, ?)",
                (name, data, time()),
            )

    # ---- pin / 事务 / 批量校验 ----------------------------------------------------------

    def _db_marks(self) -> tuple[int, ...]:
        connection = self._core.reader_connection()
        data_version = (
            0 if connection is None else int(connection.execute("PRAGMA data_version").fetchone()[0])
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
                "SELECT digest, byte_length, encoding, external, bytes FROM objects"
                f" WHERE digest IN ({marks})",
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
    文件布局只读回退;旧 ``.claim`` 文件仍按 ADR 0023 的 mtime 规则被尊重。"""

    kind: Literal["files", "sqlite"] = "sqlite"

    def __init__(
        self,
        cache_dir: Path,
        *,
        db_name: str = "model-cache.sqlite",
        synchronous: str = "FULL",
    ) -> None:
        self.cache_dir = cache_dir
        self._files = FileModelCacheBackend(cache_dir)
        self._core = _SqliteCore(cache_dir / db_name, MODEL_CACHE_SCHEMA, synchronous=synchronous)

    # ---- 记录 / 响应 / 上下文 ----------------------------------------------------------

    def record(self, key: str) -> bytes | None:
        row = (
            self._core.connection()
            .execute("SELECT record FROM requests WHERE record_key = ?", (key,))
            .fetchone()
        )
        if row is not None:
            return bytes(row[0])
        return self._files.record(key)

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

    def response(self, digest: str) -> bytes | None:
        _require_digest(digest)
        row = (
            self._core.connection()
            .execute("SELECT encoding, bytes FROM responses WHERE digest = ?", (digest,))
            .fetchone()
        )
        if row is None:
            return self._files.response(digest)
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
            return self._files.context(fingerprint)
        return _decode(bytes(row[1]), str(row[0]))

    def put_context(self, fingerprint: str, data: bytes) -> None:
        _require_digest(fingerprint)
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

    # ---- claim / release(ADR 0023 的 claims 表)---------------------------------------

    def claim(self, key: str, owner: ClaimOwner) -> int | None:
        legacy = self._legacy_claim_blocks(key, owner)
        if legacy:
            return None
        payload = lease.owner_payload(
            CLAIM_FORMAT, owner, extra={"request_fingerprint": key.partition(".")[0]}
        ).decode()
        with self._core.transaction():
            connection = self._core.connection()
            row = connection.execute(
                "SELECT generation, claim, created_at FROM claims WHERE record_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO claims (record_key, generation, claim, host, pid, process,"
                    " created_at, lease_seconds) VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        payload,
                        owner.host,
                        owner.pid,
                        owner.process,
                        owner.created_at,
                        owner.lease_seconds,
                    ),
                )
                return 0
            generation, held, held_created = int(row[0]), str(row[1]), float(row[2])
            if not lease.lease_expired(
                held.encode(),
                held_created,
                claim_format=CLAIM_FORMAT,
                process_token=owner.process,
            ):
                return None
            cursor = connection.execute(
                "UPDATE claims SET generation = ?, claim = ?, host = ?, pid = ?, process = ?,"
                " created_at = ?, lease_seconds = ? WHERE record_key = ? AND generation = ?",
                (
                    generation + 1,
                    payload,
                    owner.host,
                    owner.pid,
                    owner.process,
                    owner.created_at,
                    owner.lease_seconds,
                    key,
                    generation,
                ),
            )
            return generation + 1 if cursor.rowcount == 1 else None

    def _legacy_claim_blocks(self, key: str, owner: ClaimOwner) -> bool:
        """旧代码留下的 ``.claim`` 文件:持有者还可能在跑就挡住(ADR 0023 的 mtime 规则)。"""
        base = self.cache_dir / "requests" / f"{key}.json.claim"
        holder = lease.generation_path(base, lease.latest_generation(base))
        try:
            held = holder.read_bytes()
            modified = holder.stat().st_mtime
        except OSError:
            return False
        return not lease.lease_expired(
            held, modified, claim_format=CLAIM_FORMAT, process_token=owner.process
        )

    def release(self, key: str, owner: ClaimOwner) -> None:
        with self._core.transaction():
            self._core.connection().execute(
                "DELETE FROM claims WHERE record_key = ? AND process = ?",
                (key, owner.process),
            )

    def transaction(self) -> AbstractContextManager[None]:
        return self._core.transaction()

    def close(self) -> None:
        self._core.close()


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
