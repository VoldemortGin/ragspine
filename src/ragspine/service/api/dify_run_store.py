"""Dify 公共 Workflow API 的终态运行摘要存储（ADR 0026）。

两种实现共用 `DifyRunStore` 协议（owner = API key 的 SHA-256 摘要，由调用方传入）：
- `InMemoryRunStore`：默认；进程内、全局最近 `max_runs` 条，按写入顺序淘汰（旧行为）。
- `SqliteRunStore`：显式 opt-in（`RAGSPINE_DIFY_PUBLIC_RUN_STORE_PATH`）；stdlib sqlite3、
  每次操作短生命周期连接，事务内写入并按 owner 淘汰（每 owner 最近 `max_runs_per_owner` 条），
  JSON 序列化（绝不 pickle）。

只保存已完成 run 的公开摘要，不是 checkpoint / 恢复点。摘要含业务输入输出，是明文授权
应用数据，不进隐私 trace；原始 bearer key 从不落库。任何存储故障（打不开、损坏、结构
不兼容、序列化失败、提交失败）统一抛 `HistoryUnavailable`，消息不含路径 / key / 正文。
"""

import hashlib
import json
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

DEFAULT_MAX_RUNS = 100

_SCHEMA_VERSION = 1
_TABLE = "dify_public_runs"
_BUSY_TIMEOUT_S = 10.0


class HistoryUnavailable(Exception):
    """运行历史存储不可用 / 损坏；API 层统一转为 503 history_unavailable。"""

    def __init__(self, reason: str = "storage_error") -> None:
        # reason 只放固定代码，绝不拼接路径、key 或业务正文。
        super().__init__(f"workflow run history unavailable ({reason})")
        self.reason = reason


def owner_digest(api_key: str) -> str:
    """API key 的单向摘要（SHA-256 hex），作为历史记录的归属标识。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


@runtime_checkable
class DifyRunStore(Protocol):
    """终态运行摘要存储：查询同时约束 owner 与 run_id。"""

    def save(self, owner: str, run_id: str, payload: dict[str, Any]) -> None: ...

    def get(self, owner: str, run_id: str) -> dict[str, Any] | None: ...


class InMemoryRunStore:
    """进程内默认实现：全局最近 max_runs 条，按写入顺序淘汰，读取不提升。"""

    def __init__(self, max_runs: int = DEFAULT_MAX_RUNS) -> None:
        self._max_runs = max_runs
        self._runs: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def save(self, owner: str, run_id: str, payload: dict[str, Any]) -> None:
        key = (owner, run_id)
        self._runs.pop(key, None)
        self._runs[key] = payload
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)

    def get(self, owner: str, run_id: str) -> dict[str, Any] | None:
        return self._runs.get((owner, run_id))


class SqliteRunStore:
    """SQLite 文件实现：短连接、事务写入 + 每 owner 淘汰、JSON 序列化。

    不创建父目录、不覆盖陌生 / 更新版本的库：遇到非本 schema 的文件一律
    HistoryUnavailable，保持原文件不动。
    """

    def __init__(self, path: str | Path, max_runs_per_owner: int = DEFAULT_MAX_RUNS) -> None:
        self._path = Path(path)
        self._max_runs_per_owner = max_runs_per_owner

    def save(self, owner: str, run_id: str, payload: dict[str, Any]) -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HistoryUnavailable("serialization_error") from exc
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn, create=True)
            conn.execute(
                f"INSERT INTO {_TABLE} (owner, run_id, payload) VALUES (?, ?, ?)",
                (owner, run_id, text),
            )
            conn.execute(
                f"DELETE FROM {_TABLE} WHERE owner = ? AND seq NOT IN ("
                f"SELECT seq FROM {_TABLE} WHERE owner = ? ORDER BY seq DESC LIMIT ?)",
                (owner, owner, self._max_runs_per_owner),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise HistoryUnavailable("write_error") from exc
        finally:
            conn.close()  # 未提交的事务随关闭回滚

    def get(self, owner: str, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            if not self._ensure_schema(conn, create=False):
                return None  # 新库尚未写入：读取不建表
            row = conn.execute(
                f"SELECT payload FROM {_TABLE} WHERE owner = ? AND run_id = ?",
                (owner, run_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryUnavailable("read_error") from exc
        finally:
            conn.close()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise HistoryUnavailable("corrupt_record") from exc
        if not isinstance(payload, dict):
            raise HistoryUnavailable("corrupt_record")
        return payload

    def _connect(self) -> sqlite3.Connection:
        # 父目录不存在 / 路径是目录：直接判不可用，不隐式建目录。
        if not self._path.parent.is_dir() or self._path.is_dir():
            raise HistoryUnavailable("unavailable_path")
        try:
            # isolation_level=None：事务全部显式 BEGIN / COMMIT；每次操作一条短连接。
            return sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        except sqlite3.Error as exc:
            raise HistoryUnavailable("unavailable_path") from exc

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection, *, create: bool) -> bool:
        """校验 schema，返回表是否存在。

        空库：create=True 时建表并标版本，否则返回 False；已有库只接受本版本 schema，
        陌生表 / 其他版本 / 缺列一律拒绝（不改动原文件）。
        """
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version == 0:
            (objects,) = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            if objects:
                raise HistoryUnavailable("incompatible_schema")
            if not create:
                return False
            conn.execute(
                f"CREATE TABLE {_TABLE} ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "owner TEXT NOT NULL, "
                "run_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, "
                "UNIQUE (owner, run_id))"
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return True
        if version != _SCHEMA_VERSION:
            raise HistoryUnavailable("incompatible_schema")
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
        if not {"seq", "owner", "run_id", "payload"} <= columns:
            raise HistoryUnavailable("incompatible_schema")
        return True
