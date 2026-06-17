"""Ingestion manifest 台账 + 可观测指标 + 生产配置版本清单（运维/管理面）。

跑批健康状况的唯一台账与观测入口（PRD user stories #30/#31/#33）：

    ManifestStore —— 每批一条 manifest 记录，含输入清单（path/hash/format）、
        产出事实数、告警数、失败项与耗时；open_batch / record_input / close_batch
        构成一批的生命周期，沿用 sqlite（与 fact_store 同库不同表）。
    compute_metrics —— 跨 manifest/queue/store 汇总关键指标：各通道抽取量、告警率、
        复核积压数、置信度分布桶，供「异常尽早发现」。
    list_versions —— 生产配置清单：事实表里出现过的 extractor_version 集合 +
        registry 各 scope 当前 active 的映射版本，回答「现在生产用的是哪套配置」。

实现已完成，dataclass 字段契约保持冻结，行为契约见 tests/ingestion/structured/test_manifest.py。
"""

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.storage.fact_store import FactStore

# 批次状态机取值
BATCH_RUNNING = "running"
BATCH_DONE = "done"
BATCH_FAILED = "failed"

# 置信度分布桶边界（user story #31）。
_BUCKET_LOW = "<0.5"
_BUCKET_MID = "0.5-0.8"
_BUCKET_HIGH = ">=0.8"


@dataclass
class ManifestRecord:
    """一批 ingestion 的台账记录（user story #30）。

    字段语义约定：
        batch_id:    批次主键（open_batch 时分配，字符串）。
        started_at:  批次开始时间 ISO 串。
        finished_at: 批次结束时间 ISO 串（close_batch 前为 None）。
        status:      running / done / failed。
        inputs:      输入清单，每项 dict 含 path / hash / format（record_input 累加）。
        n_facts:     本批产出事实总数。
        n_warnings:  本批告警总数。
        n_failed:    本批失败的输入文件数。
        duration_s:  耗时秒数（close_batch 时算出）。
        failures:    失败明细列表（每项含 path / error 等）。
    """

    batch_id: str
    started_at: str | None = None
    finished_at: str | None = None
    status: str = BATCH_RUNNING
    inputs: list[dict[str, str | None]] = field(default_factory=list)
    n_facts: int = 0
    n_warnings: int = 0
    n_failed: int = 0
    duration_s: float | None = None
    failures: list[dict[str, str | None]] = field(default_factory=list)


class ManifestStore:
    """ingestion manifest 台账读写（sqlite）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """建 manifest 主表（manifest_batch）+ 输入清单表（manifest_input）+ 索引。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifest_batch (
                batch_id    TEXT PRIMARY KEY,
                started_at  TEXT,
                finished_at TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                duration_s  REAL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifest_input (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id   TEXT NOT NULL,
                path       TEXT NOT NULL,
                hash       TEXT,
                format     TEXT,
                n_facts    INTEGER NOT NULL DEFAULT 0,
                n_warnings INTEGER NOT NULL DEFAULT 0,
                failed     INTEGER NOT NULL DEFAULT 0,
                error      TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_manifest_input_batch "
            "ON manifest_input (batch_id, seq)"
        )
        self._conn.commit()

    def open_batch(self, batch_id: str | None = None) -> str:
        """开一批：写入 started_at、status='running'，返回 batch_id（未传则自动分配）。"""
        if batch_id is None:
            batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO manifest_batch (batch_id, started_at, status) VALUES (?, ?, ?)",
            (batch_id, _now_iso(), BATCH_RUNNING),
        )
        self._conn.commit()
        return batch_id

    def record_input(
        self,
        batch_id: str,
        path: str,
        file_hash: str | None,
        fmt: str,
        *,
        n_facts: int = 0,
        n_warnings: int = 0,
        failed: bool = False,
        error: str | None = None,
    ) -> None:
        """登记一个输入文件到某批：path/hash/format + 该文件产出/告警/是否失败。

        累加进该批的 n_facts / n_warnings；failed=True 时计入 n_failed 与 failures。
        单文件失败不影响整批继续（user story #19 的台账侧支撑）。
        """
        self._conn.execute(
            "INSERT INTO manifest_input "
            "(batch_id, path, hash, format, n_facts, n_warnings, failed, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                path,
                file_hash,
                fmt,
                n_facts,
                n_warnings,
                1 if failed else 0,
                error,
            ),
        )
        self._conn.commit()

    def close_batch(
        self,
        batch_id: str,
        status: str = BATCH_DONE,
    ) -> None:
        """收尾一批：写 finished_at、最终 status、算出 duration_s。"""
        row = self._conn.execute(
            "SELECT started_at FROM manifest_batch WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        finished_at = _now_iso()
        duration_s = None
        if row is not None and row["started_at"]:
            duration_s = max(0.0, _parse_iso(finished_at) - _parse_iso(row["started_at"]))
        self._conn.execute(
            "UPDATE manifest_batch "
            "SET finished_at = ?, status = ?, duration_s = ? WHERE batch_id = ?",
            (finished_at, status, duration_s, batch_id),
        )
        self._conn.commit()

    def get_batch(self, batch_id: str) -> ManifestRecord | None:
        """按 batch_id 取完整台账记录（含 inputs / 汇总数 / failures）；无则 None。"""
        row = self._conn.execute(
            "SELECT * FROM manifest_batch WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        return self._build_record(row)

    def list_batches(self) -> list[ManifestRecord]:
        """列出全部批次，按开始时间序返回（运维总览）。"""
        rows = self._conn.execute(
            "SELECT * FROM manifest_batch ORDER BY started_at ASC, batch_id ASC"
        ).fetchall()
        return [self._build_record(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    # --- 内部：从行装配完整 ManifestRecord（含输入清单汇总）-----------------

    def _build_record(self, batch_row: sqlite3.Row) -> ManifestRecord:
        inputs_rows = self._conn.execute(
            "SELECT * FROM manifest_input WHERE batch_id = ? ORDER BY seq ASC",
            (batch_row["batch_id"],),
        ).fetchall()
        inputs: list[dict[str, str | None]] = []
        failures: list[dict[str, str | None]] = []
        n_facts = 0
        n_warnings = 0
        n_failed = 0
        for r in inputs_rows:
            inputs.append(
                {"path": r["path"], "hash": r["hash"], "format": r["format"]}
            )
            n_facts += r["n_facts"] or 0
            n_warnings += r["n_warnings"] or 0
            if r["failed"]:
                n_failed += 1
                failures.append({"path": r["path"], "error": r["error"]})
        return ManifestRecord(
            batch_id=batch_row["batch_id"],
            started_at=batch_row["started_at"],
            finished_at=batch_row["finished_at"],
            status=batch_row["status"],
            inputs=inputs,
            n_facts=n_facts,
            n_warnings=n_warnings,
            n_failed=n_failed,
            duration_s=batch_row["duration_s"],
            failures=failures,
        )


def compute_metrics(
    manifest_store: ManifestStore, queue: ReviewQueue, store: FactStore
) -> dict[str, Any]:
    """汇总关键可观测指标（user story #31）。

    跨 manifest / 复核队列 / 事实表计算并返回 dict，至少包含：
        - 抽取量：各批 / 总计产出事实数（如 'n_facts_total'、按通道/批次细分）。
        - 告警率：告警数 / 处理输入数（如 'warning_rate'）。
        - 复核积压数：当前 pending 复核项数（如 'review_backlog'）。
        - 置信度分布桶：事实 confidence 落入若干区间的计数
          （如 'confidence_buckets': {'<0.5': n, '0.5-0.8': n, '>=0.8': n}）。

    便于异常尽早发现：积压突增 / 告警率飙升 / 低置信占比变大都能从返回 dict 读出。
    """
    n_facts_total = store.count()
    review_backlog = len(queue.list_pending())

    # 置信度分布桶：扫描事实表里带 confidence 的事实。
    buckets = {_BUCKET_LOW: 0, _BUCKET_MID: 0, _BUCKET_HIGH: 0}
    for (conf,) in store.execute_read(
        "SELECT confidence FROM fact_metric WHERE confidence IS NOT NULL"
    ):
        if conf < 0.5:
            buckets[_BUCKET_LOW] += 1
        elif conf < 0.8:
            buckets[_BUCKET_MID] += 1
        else:
            buckets[_BUCKET_HIGH] += 1

    # 告警率：所有批次累计告警数 / 处理输入数。
    n_inputs = 0
    n_warnings = 0
    for batch in manifest_store.list_batches():
        n_inputs += len(batch.inputs)
        n_warnings += batch.n_warnings
    warning_rate = (n_warnings / n_inputs) if n_inputs else 0.0

    return {
        "n_facts_total": n_facts_total,
        "review_backlog": review_backlog,
        "confidence_buckets": buckets,
        "warning_rate": warning_rate,
        "n_inputs_processed": n_inputs,
        "n_warnings_total": n_warnings,
    }


def list_versions(store: FactStore, registry: MappingRegistry) -> dict[str, Any]:
    """生产配置版本清单（user story #33）。

    返回 dict，至少包含：
        - 'extractor_versions'：事实表中出现过的 extractor_version 去重清单
          （知道历史 / 当前用过哪些抽取器版本）。
        - 'active_mappings'：registry 各 scope 当前 active 的映射版本
          （如 {scope: version}），回答「现在生产用的是哪套颜色映射」。
    """
    extractor_versions = [
        row[0]
        for row in store.execute_read(
            "SELECT DISTINCT extractor_version FROM fact_metric "
            "WHERE extractor_version IS NOT NULL ORDER BY extractor_version"
        )
    ]

    active_mappings: dict[str, int] = {}
    for (scope,) in registry.execute_read(
        "SELECT DISTINCT scope FROM color_mapping WHERE status = 'active'"
    ):
        mapping = registry.get_active(scope)
        if mapping is not None:
            active_mappings[scope] = mapping.version

    return {
        "extractor_versions": extractor_versions,
        "active_mappings": active_mappings,
    }


def _now_iso() -> str:
    """当前 UTC 时间 ISO 串。"""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> float:
    """ISO 时间串转 epoch 秒（计算 duration 用）。"""
    return datetime.fromisoformat(value).timestamp()
