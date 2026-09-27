"""Dify public API 运行历史存储的聚焦测试（ADR 0026 / 测试计划 HIST-008～HIST-018）。

覆盖存储层契约：owner 摘要、内存 / SQLite 两种实现的读写与归属、容量淘汰顺序、
JSON round-trip、损坏 / 不兼容 / 不可用存储统一抛 HistoryUnavailable、注入提交失败
无半条记录、多线程写入、短生命周期连接（关闭后可重命名删除）。API 层行为见
test_api_dify_public.py。
"""

import hashlib
import os
import sqlite3
import threading

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.service.api.dify_run_store import (
    HistoryUnavailable,
    InMemoryRunStore,
    SqliteRunStore,
    owner_digest,
)

OWNER_A = owner_digest("app-key-a")
OWNER_B = owner_digest("app-key-b")


def _payload(run_id: str, **extra) -> dict:
    payload = {
        "id": run_id,
        "workflow_id": "wf-1",
        "status": "succeeded",
        "inputs": {"question": "hi"},
        "outputs": {"result": "ok"},
        "error": None,
        "total_steps": 4,
        "total_tokens": 0,
        "created_at": 1_700_000_000,
        "finished_at": 1_700_000_001,
        "elapsed_time": 0.25,
    }
    payload.update(extra)
    return payload


@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    if request.param == "memory":
        return InMemoryRunStore()
    return SqliteRunStore(tmp_path / "runs.db")


# ---------------------------------------------------------------------------
# owner 摘要（HIST-008）
# ---------------------------------------------------------------------------
def test_owner_digest_is_sha256_hex_and_stable():
    key = "app-key-secret"
    assert owner_digest(key) == hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert owner_digest(key) == owner_digest(key)
    assert key not in owner_digest(key)
    assert owner_digest("a") != owner_digest("b")


# ---------------------------------------------------------------------------
# 两种实现共享的基本契约
# ---------------------------------------------------------------------------
def test_save_then_get_round_trips(any_store):
    any_store.save(OWNER_A, "run-1", _payload("run-1"))
    assert any_store.get(OWNER_A, "run-1") == _payload("run-1")


def test_get_is_scoped_by_owner_and_unknown_is_none(any_store):
    any_store.save(OWNER_A, "run-1", _payload("run-1"))
    assert any_store.get(OWNER_B, "run-1") is None
    assert any_store.get(OWNER_A, "no-such-run") is None


def test_default_capacities_are_100(tmp_path):
    memory = InMemoryRunStore()
    for i in range(101):
        memory.save(OWNER_A, f"r{i}", _payload(f"r{i}"))
    assert memory.get(OWNER_A, "r0") is None
    assert memory.get(OWNER_A, "r1") is not None

    sqlite_store = SqliteRunStore(tmp_path / "runs.db")
    for i in range(101):
        sqlite_store.save(OWNER_A, f"r{i}", _payload(f"r{i}"))
    assert sqlite_store.get(OWNER_A, "r0") is None
    assert sqlite_store.get(OWNER_A, "r1") is not None


# ---------------------------------------------------------------------------
# 内存实现：保持旧的全局 FIFO 语义
# ---------------------------------------------------------------------------
def test_memory_capacity_is_global_fifo_and_reads_do_not_promote():
    store = InMemoryRunStore(max_runs=2)
    store.save(OWNER_A, "a1", _payload("a1"))
    store.save(OWNER_B, "b1", _payload("b1"))
    assert store.get(OWNER_A, "a1") is not None  # 读取不提升
    store.save(OWNER_A, "a2", _payload("a2"))
    assert store.get(OWNER_A, "a1") is None
    assert store.get(OWNER_B, "b1") is not None
    assert store.get(OWNER_A, "a2") is not None


def test_memory_store_does_not_persist_across_instances():
    InMemoryRunStore().save(OWNER_A, "run-1", _payload("run-1"))
    assert InMemoryRunStore().get(OWNER_A, "run-1") is None


# ---------------------------------------------------------------------------
# SQLite：跨实例持久、每 owner 容量（HIST-009 / HIST-010）
# ---------------------------------------------------------------------------
def test_sqlite_new_instance_reads_same_file(tmp_path):
    path = tmp_path / "runs.db"
    SqliteRunStore(path).save(OWNER_A, "run-1", _payload("run-1"))
    assert SqliteRunStore(path).get(OWNER_A, "run-1") == _payload("run-1")


def test_sqlite_accepts_str_path(tmp_path):
    path = str(tmp_path / "runs.db")
    SqliteRunStore(path).save(OWNER_A, "run-1", _payload("run-1"))
    assert SqliteRunStore(path).get(OWNER_A, "run-1") is not None


def test_hist_009_per_owner_fifo_reads_do_not_promote(tmp_path):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path, max_runs_per_owner=2)
    store.save(OWNER_A, "A", _payload("A"))
    store.save(OWNER_A, "B", _payload("B"))
    assert store.get(OWNER_A, "A") is not None
    store.save(OWNER_A, "C", _payload("C"))

    reopened = SqliteRunStore(path, max_runs_per_owner=2)
    assert reopened.get(OWNER_A, "A") is None
    assert reopened.get(OWNER_A, "B") is not None
    assert reopened.get(OWNER_A, "C") is not None


def test_hist_009_identical_timestamps_evict_by_insertion_order(tmp_path):
    store = SqliteRunStore(tmp_path / "runs.db", max_runs_per_owner=2)
    # 故意倒序的 run ID + 完全相同的时间戳：只能按写入顺序淘汰。
    for run_id in ("zzz", "mmm", "aaa"):
        store.save(OWNER_A, run_id, _payload(run_id, created_at=1, finished_at=1))
    assert store.get(OWNER_A, "zzz") is None
    assert store.get(OWNER_A, "mmm") is not None
    assert store.get(OWNER_A, "aaa") is not None


def test_hist_010_owners_do_not_evict_each_other(tmp_path):
    store = SqliteRunStore(tmp_path / "runs.db", max_runs_per_owner=2)
    store.save(OWNER_A, "a1", _payload("a1"))
    store.save(OWNER_A, "a2", _payload("a2"))
    store.save(OWNER_B, "b1", _payload("b1"))
    store.save(OWNER_B, "b2", _payload("b2"))
    store.save(OWNER_A, "a3", _payload("a3"))
    assert store.get(OWNER_A, "a1") is None
    assert [store.get(OWNER_A, r) is not None for r in ("a2", "a3")] == [True, True]
    assert [store.get(OWNER_B, r) is not None for r in ("b1", "b2")] == [True, True]


def test_hist_010_separate_files_do_not_evict_each_other(tmp_path):
    one = SqliteRunStore(tmp_path / "one.db", max_runs_per_owner=1)
    two = SqliteRunStore(tmp_path / "two.db", max_runs_per_owner=1)
    one.save(OWNER_A, "r1", _payload("r1"))
    two.save(OWNER_A, "r2", _payload("r2"))
    assert one.get(OWNER_A, "r1") is not None
    assert two.get(OWNER_A, "r2") is not None


def test_same_run_id_under_two_owners_is_kept_apart(tmp_path):
    store = SqliteRunStore(tmp_path / "runs.db")
    store.save(OWNER_A, "same", _payload("same", outputs={"who": "a"}))
    store.save(OWNER_B, "same", _payload("same", outputs={"who": "b"}))
    assert store.get(OWNER_A, "same")["outputs"] == {"who": "a"}
    assert store.get(OWNER_B, "same")["outputs"] == {"who": "b"}


# ---------------------------------------------------------------------------
# HIST-008：数据库字节不含原始 key（owner 由调用方摘要后传入）
# ---------------------------------------------------------------------------
def test_hist_008_database_bytes_hold_digest_not_key(tmp_path):
    sentinel = "store-HIST008-sentinel-1c9e4f7a"
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    store.save(owner_digest(sentinel), "run-1", _payload("run-1"))
    assert store.get(owner_digest(sentinel), "run-1") is not None
    for sidecar in path.parent.iterdir():
        if sidecar.name.startswith(path.name):
            assert sentinel.encode("utf-8") not in sidecar.read_bytes()


# ---------------------------------------------------------------------------
# HIST-011：空格 + 中文路径
# ---------------------------------------------------------------------------
def test_hist_011_unicode_space_path(tmp_path):
    folder = tmp_path / "运行 历史"
    folder.mkdir()
    path = folder / "记录 文件.db"
    SqliteRunStore(path).save(OWNER_A, "run-1", _payload("run-1"))
    assert path.is_file()
    assert SqliteRunStore(path).get(OWNER_A, "run-1") is not None


# ---------------------------------------------------------------------------
# HIST-012：JSON round-trip 保持类型；非法 JSON 值显式失败、无部分记录
# ---------------------------------------------------------------------------
def test_hist_012_nested_values_keep_types(tmp_path):
    path = tmp_path / "runs.db"
    outputs = {
        "中文": "值",
        "nested": {"list": [1, 2.5, None, True, False, {"k": []}]},
        "int": 7,
        "float": 3.25,
        "neg_zero": -0.0,
        "none": None,
    }
    SqliteRunStore(path).save(OWNER_A, "run-1", _payload("run-1", outputs=outputs))
    got = SqliteRunStore(path).get(OWNER_A, "run-1")["outputs"]
    assert got == outputs
    assert type(got["int"]) is int
    assert type(got["float"]) is float
    assert type(got["nested"]["list"]) is list
    assert got["nested"]["list"][3] is True


@pytest.mark.parametrize(
    "bad_value", [float("nan"), float("inf"), {1, 2}, object()], ids=["nan", "inf", "set", "obj"]
)
def test_hist_012_non_json_values_raise_and_write_nothing(tmp_path, bad_value):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    with pytest.raises(HistoryUnavailable):
        store.save(OWNER_A, "run-1", _payload("run-1", outputs={"bad": bad_value}))
    assert store.get(OWNER_A, "run-1") is None


# ---------------------------------------------------------------------------
# HIST-013：不可用 / 损坏 / 不兼容 -> HistoryUnavailable，且不改动原文件
# ---------------------------------------------------------------------------
def _assert_redacted(exc: HistoryUnavailable, *secrets: str) -> None:
    text = str(exc)
    for secret in secrets:
        assert secret not in text


def test_hist_013_directory_path_raises(tmp_path):
    path = tmp_path / "a-directory.db"
    path.mkdir()
    store = SqliteRunStore(path)
    with pytest.raises(HistoryUnavailable) as exc_info:
        store.save(OWNER_A, "run-1", _payload("run-1"))
    _assert_redacted(exc_info.value, str(path), path.name)
    with pytest.raises(HistoryUnavailable):
        store.get(OWNER_A, "run-1")
    assert list(path.iterdir()) == []


def test_hist_013_missing_parent_raises_and_is_not_created(tmp_path):
    path = tmp_path / "missing" / "runs.db"
    with pytest.raises(HistoryUnavailable) as exc_info:
        SqliteRunStore(path).save(OWNER_A, "run-1", _payload("run-1"))
    _assert_redacted(exc_info.value, str(path))
    assert not path.parent.exists()


def test_hist_013_corrupt_file_raises_and_is_untouched(tmp_path):
    path = tmp_path / "runs.db"
    garbage = b"garbage, not sqlite \x00\x01\xfe" * 300
    path.write_bytes(garbage)
    store = SqliteRunStore(path)
    with pytest.raises(HistoryUnavailable):
        store.save(OWNER_A, "run-1", _payload("run-1"))
    with pytest.raises(HistoryUnavailable):
        store.get(OWNER_A, "run-1")
    assert path.read_bytes() == garbage


@pytest.mark.parametrize("variant", ["future_version", "foreign_table", "wrong_columns"])
def test_hist_013_incompatible_schema_raises_and_is_untouched(tmp_path, variant):
    path = tmp_path / "runs.db"
    conn = sqlite3.connect(path)
    try:
        if variant == "future_version":
            conn.execute("PRAGMA user_version = 999")
            conn.execute("CREATE TABLE placeholder (x TEXT)")
        elif variant == "foreign_table":
            conn.execute("CREATE TABLE other_app (payload TEXT)")
        else:
            conn.execute("CREATE TABLE dify_public_runs (run_id TEXT)")
            conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
    original = path.read_bytes()
    store = SqliteRunStore(path)
    with pytest.raises(HistoryUnavailable):
        store.save(OWNER_A, "run-1", _payload("run-1"))
    with pytest.raises(HistoryUnavailable):
        store.get(OWNER_A, "run-1")
    assert path.read_bytes() == original


# ---------------------------------------------------------------------------
# HIST-014：已归属记录损坏 -> 该 owner HistoryUnavailable；其它 owner 先按归属筛选为 None
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["{broken", "[]", '"just a string"', "null"])
def test_hist_014_corrupt_payload_raises_for_owner_only(tmp_path, raw):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    store.save(OWNER_A, "run-1", _payload("run-1"))
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE dify_public_runs SET payload = ? WHERE run_id = ?", (raw, "run-1"))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(HistoryUnavailable) as exc_info:
        store.get(OWNER_A, "run-1")
    _assert_redacted(exc_info.value, raw, str(path))
    assert store.get(OWNER_B, "run-1") is None


# ---------------------------------------------------------------------------
# HIST-015：注入提交失败 -> HistoryUnavailable，无部分记录，既有记录可读
# ---------------------------------------------------------------------------
class _FailingCommitConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith(("COMMIT", "END")):
            raise sqlite3.OperationalError("injected commit failure")
        return super().execute(sql, *args, **kwargs)

    def commit(self):
        raise sqlite3.OperationalError("injected commit failure")


def test_hist_015_commit_failure_leaves_no_partial_record(tmp_path, monkeypatch):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path, max_runs_per_owner=1)
    store.save(OWNER_A, "kept", _payload("kept"))

    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        kwargs["factory"] = _FailingCommitConnection
        return real_connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", _connect)
        with pytest.raises(HistoryUnavailable):
            store.save(OWNER_A, "lost", _payload("lost"))

    # 容量 1：若淘汰先于失败的提交生效，kept 会丢；事务回滚后必须原样保留。
    assert store.get(OWNER_A, "kept") == _payload("kept")
    assert store.get(OWNER_A, "lost") is None


def test_hist_015_duplicate_run_id_for_owner_raises(tmp_path):
    store = SqliteRunStore(tmp_path / "runs.db")
    store.save(OWNER_A, "run-1", _payload("run-1"))
    with pytest.raises(HistoryUnavailable):
        store.save(OWNER_A, "run-1", _payload("run-1", status="failed"))
    assert store.get(OWNER_A, "run-1")["status"] == "succeeded"


# ---------------------------------------------------------------------------
# HIST-016：多线程并发写入同一文件，无 thread-affinity 异常，容量有界
# ---------------------------------------------------------------------------
def test_hist_016_concurrent_thread_writes(tmp_path):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path, max_runs_per_owner=10)
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def _worker(t: int) -> None:
        try:
            barrier.wait()
            for i in range(5):
                store.save(OWNER_A, f"t{t}-{i}", _payload(f"t{t}-{i}"))
                store.save(OWNER_B, f"t{t}-{i}", _payload(f"t{t}-{i}"))
        except BaseException as exc:  # noqa: BLE001 —— 汇总到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    conn = sqlite3.connect(path)
    try:
        rows = dict(
            conn.execute("SELECT owner, COUNT(*) FROM dify_public_runs GROUP BY owner").fetchall()
        )
    finally:
        conn.close()
    assert rows == {OWNER_A: 10, OWNER_B: 10}


# ---------------------------------------------------------------------------
# HIST-018：短生命周期连接——操作后即可重命名 / 删除文件
# ---------------------------------------------------------------------------
def test_hist_018_file_can_be_renamed_and_deleted_after_use(tmp_path):
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    store.save(OWNER_A, "run-1", _payload("run-1"))
    assert store.get(OWNER_A, "run-1") is not None
    moved = path.with_name("moved.db")
    path.rename(moved)
    moved.unlink()
    assert not moved.exists()
    # 同一实例不缓存连接：原路径上重新建库，旧记录不存在。
    assert store.get(OWNER_A, "run-1") is None
