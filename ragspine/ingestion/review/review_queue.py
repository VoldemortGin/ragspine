"""人工复核队列（sqlite，与 fact_store 同库不同表）。

低置信 / 不一致 / 未确认项进入队列，SME 一键通过 / 驳回并自动留痕（谁、何时、依据），
驳回可附更正值直接生效。状态机 pending → approved / rejected，审计采用追加式记录
（user stories 22–25、28）。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 状态机取值
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# 终态集合：进入后不可再流转。
_TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED)


@dataclass
class ReviewItem:
    """复核队列中的一条待审 / 已审项。

    字段：
        id:              队列项主键（enqueue 时分配）。
        reason:          入队原因（如 'low_confidence' / 'cross_channel_mismatch' /
                         'unconfirmed_mapping'）。
        payload:         待审载荷（dict，如抽取出的候选 fact / 双通道两侧结果）。
        locator:         原文精确定位回链（如 'sheet=HK!C4'，供 SME 不翻原件即可核对）。
        priority:        优先级（数值，越小越先审；list_pending 按此排序）。
        status:          pending / approved / rejected。
        actor:           处理人（approve / reject 时写入）。
        note:            处理备注。
        corrected_value: 驳回时附带的更正值（user story 25），可为 None。
    """

    reason: str
    payload: dict
    locator: str
    priority: int = 100
    id: int | None = None
    status: str = STATUS_PENDING
    actor: str | None = None
    note: str | None = None
    corrected_value: object | None = None


@dataclass
class AuditRecord:
    """一条追加式审计记录（不可篡改的时间序，user story 28）。

    字段：
        item_id:    关联的 ReviewItem id。
        action:     'enqueue' / 'approve' / 'reject'。
        actor:      操作人。
        at:         操作时间 ISO 串。
        note:       备注。
        detail:     附加细节（如更正值），dict。
    """

    item_id: int
    action: str
    actor: str | None = None
    at: str | None = None
    note: str | None = None
    detail: dict = field(default_factory=dict)


class IllegalTransitionError(Exception):
    """非法状态流转（终态项再处理 / 处理不存在的项）。"""


# 哨兵：区分"未传更正值"与"更正值显式为 None"。
_UNSET = object()


class ReviewQueue:
    """复核队列读写（sqlite）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """建队列表（review_item）+ 追加式审计表（review_audit）+ 索引。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_item (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                reason          TEXT NOT NULL,
                payload         TEXT NOT NULL,
                locator         TEXT NOT NULL,
                priority        INTEGER NOT NULL DEFAULT 100,
                status          TEXT NOT NULL DEFAULT 'pending',
                actor           TEXT,
                note            TEXT,
                corrected_value TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_audit (
                seq     INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action  TEXT NOT NULL,
                actor   TEXT,
                at      TEXT NOT NULL,
                note    TEXT,
                detail  TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_review_item_status "
            "ON review_item (status, priority)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_review_audit_item "
            "ON review_audit (item_id, seq)"
        )
        self._conn.commit()

    def enqueue(
        self,
        reason: str,
        payload: dict,
        locator: str,
        priority: int = 100,
    ) -> int:
        """入队一条待审项（status=pending），写一条 enqueue 审计记录。返回新 id。"""
        cur = self._conn.execute(
            "INSERT INTO review_item (reason, payload, locator, priority, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                reason,
                json.dumps(payload, ensure_ascii=False),
                locator,
                priority,
                STATUS_PENDING,
            ),
        )
        item_id = int(cur.lastrowid)
        self._append_audit(item_id, "enqueue")
        self._conn.commit()
        return item_id

    def list_pending(self) -> list[ReviewItem]:
        """列出全部 pending 项，按 priority 升序（越小越先审）。"""
        cur = self._conn.execute(
            "SELECT * FROM review_item WHERE status = ? ORDER BY priority ASC, id ASC",
            (STATUS_PENDING,),
        )
        return [self._row_to_item(row) for row in cur.fetchall()]

    def approve(self, item_id: int, actor: str, note: str | None = None) -> None:
        """通过某项 -> status=approved，写 approve 审计记录（留痕谁/何时/依据）。"""
        self._transition(item_id, STATUS_APPROVED, "approve", actor, note, _UNSET)

    def reject(
        self,
        item_id: int,
        actor: str,
        note: str | None = None,
        corrected_value: object | None = None,
    ) -> None:
        """驳回某项 -> status=rejected，可附 corrected_value，写 reject 审计记录。"""
        self._transition(
            item_id, STATUS_REJECTED, "reject", actor, note, corrected_value
        )

    def get(self, item_id: int) -> ReviewItem | None:
        """按 id 取队列项；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM review_item WHERE id = ?", (item_id,)
        ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def audit_trail(self, item_id: int) -> list[AuditRecord]:
        """取某项的追加式审计记录，按时间序返回。"""
        cur = self._conn.execute(
            "SELECT * FROM review_audit WHERE item_id = ? ORDER BY seq ASC",
            (item_id,),
        )
        return [self._row_to_audit(row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()

    # --- 内部实现 --------------------------------------------------------

    def _transition(
        self,
        item_id: int,
        new_status: str,
        action: str,
        actor: str,
        note: str | None,
        corrected_value: object,
    ) -> None:
        """状态流转 + 追加审计；非法流转抛错且不改动既有审计（追加式不可篡改）。"""
        row = self._conn.execute(
            "SELECT status FROM review_item WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise IllegalTransitionError(f"review item {item_id} 不存在")
        if row["status"] in _TERMINAL_STATUSES:
            raise IllegalTransitionError(
                f"review item {item_id} 已是终态 {row['status']}，不可再 {action}"
            )

        detail: dict = {}
        has_correction = corrected_value is not _UNSET
        if has_correction:
            self._conn.execute(
                "UPDATE review_item SET status = ?, actor = ?, note = ?, "
                "corrected_value = ? WHERE id = ?",
                (
                    new_status,
                    actor,
                    note,
                    json.dumps(corrected_value, ensure_ascii=False),
                    item_id,
                ),
            )
            detail["corrected_value"] = corrected_value
        else:
            self._conn.execute(
                "UPDATE review_item SET status = ?, actor = ?, note = ? WHERE id = ?",
                (new_status, actor, note, item_id),
            )

        self._append_audit(item_id, action, actor=actor, note=note, detail=detail)
        self._conn.commit()

    def _append_audit(
        self,
        item_id: int,
        action: str,
        actor: str | None = None,
        note: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO review_audit (item_id, action, actor, at, note, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item_id,
                action,
                actor,
                datetime.now(timezone.utc).isoformat(),
                note,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ReviewItem:
        corrected = row["corrected_value"]
        return ReviewItem(
            id=row["id"],
            reason=row["reason"],
            payload=json.loads(row["payload"]),
            locator=row["locator"],
            priority=row["priority"],
            status=row["status"],
            actor=row["actor"],
            note=row["note"],
            corrected_value=json.loads(corrected) if corrected is not None else None,
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> AuditRecord:
        return AuditRecord(
            item_id=row["item_id"],
            action=row["action"],
            actor=row["actor"],
            at=row["at"],
            note=row["note"],
            detail=json.loads(row["detail"]) if row["detail"] else {},
        )
