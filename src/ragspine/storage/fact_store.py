"""指标事实表 fact_metric 的存储层（stdlib sqlite3）。

确定性抽取出的每个数值落成一条 Fact，带完整数据血缘（源文件 + 定位串）。
查询走参数化 SQL，命中即确定值，未命中即"查不到"，不做任何插值/推断。

v2（多模态抽取期）：Fact 增加可选的样式语义与版本血缘字段
（tags / source_file_hash / extractor_version / mapping_version / confidence /
review_status），schema 自动迁移补列；query() 默认只返回 review_status 可见的数据。
所有新字段都有默认值，旧调用（不传新字段）行为不变。
"""

import json
import sqlite3
import weakref
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

# review_status 取值：默认确定性抽取自动通过；进复核 / 被拦截 / 被驳回不可见。
REVIEW_AUTO_APPROVED = "auto_approved"
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_BLOCKED = "blocked"

# 默认对查询可见的状态（确定性抽取与人工通过）。
VISIBLE_REVIEW_STATUSES = (REVIEW_AUTO_APPROVED, REVIEW_APPROVED)

# 维度袋（dimensions）的保留名：这些名字属于结构/血缘列或样式/版本/时效列，
# 绝不能被任意维度遮蔽，否则会污染血缘与确定性身份。命中即拒。
_RESERVED_DIM_NAMES = frozenset(
    {
        "value",
        "unit",
        "source_doc_id",
        "source_locator",
        "tags",
        "review_status",
        "dim_key",
        "source_file_hash",
        "extractor_version",
        "mapping_version",
        "confidence",
        "valid_as_of",
        "ingested_at",
        "corrected_by",
        "corrected_audit_seq",
    }
)


@dataclass
class Fact:
    """一条指标事实：维度 + 数值 + 血缘（+ v2 样式语义与版本血缘）。"""

    metric_code: str
    entity: str
    geography: str
    channel: str
    period_type: str
    period: str
    value: float
    unit: str
    source_doc_id: str
    source_locator: str
    # --- v2 扩展（全部可选，默认保持旧行为）------------------------------
    tags: dict[str, object] = field(default_factory=dict)
    source_file_hash: str | None = None
    extractor_version: str | None = None
    mapping_version: int | None = None
    confidence: float | None = None
    review_status: str = REVIEW_AUTO_APPROVED
    # --- provenance 时效列（默认 None：既有构造处零改动、答案字节级不变）--------
    # valid_as_of —— 事实「截至 / 生效」业务日期（调用方 / CLI 提供，可空）。
    # ingested_at —— 写入时间戳（store 在 upsert 时统一盖 UTC ISO；此处仅读回填充）。
    valid_as_of: str | None = None
    ingested_at: str | None = None
    # --- 人工更正血缘列（默认 None：既有构造处零改动、答案字节级不变）----------
    # corrected_by —— 应用更正决议的处理人（SME），来自解析决议的审计 actor。
    # corrected_audit_seq —— 解析决议的那条 reject 审计记录 seq，applier 据此幂等。
    corrected_by: str | None = None
    corrected_audit_seq: int | None = None
    # --- 任意维度袋（内存态，不入 DB）-----------------------------------
    # 空时由 __post_init__ 从身份列派生镜像；非空时校验不与保留名冲突。
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dimensions:
            for k in self.dimensions:
                if k in _RESERVED_DIM_NAMES:
                    raise ValueError(f"dimension name {k!r} 与结构/血缘列冲突")
        else:
            self.dimensions = {
                "metric": str(self.metric_code),
                "entity": str(self.entity),
                "channel": str(self.channel),
                "period": f"{self.period_type}{self.period}",
            }


# 基础列（与原始 schema 顺序一致，旧测试依赖）。
_BASE_COLUMNS = [
    "metric_code",
    "entity",
    "geography",
    "channel",
    "period_type",
    "period",
    "value",
    "unit",
    "source_doc_id",
    "source_locator",
]
# v2 新增列（迁移时按需 ALTER 补上）。
_V2_COLUMNS = [
    "tags",
    "source_file_hash",
    "extractor_version",
    "mapping_version",
    "confidence",
    "review_status",
    # provenance 时效列（与 v2 同款幂等 ALTER 补列，旧库平滑迁移）。
    "valid_as_of",
    "ingested_at",
    # 人工更正血缘列（同款幂等 ALTER 补列）。
    "corrected_by",
    "corrected_audit_seq",
]
# Fact 的全部 dataclass 字段名（含 dimensions）。仅供需要读 Fact 字段名处使用；
# DB I/O 一律走显式列表（_DB_COLUMNS / _from_row 的字段名子集），不被此驱动。
_COLUMNS = [f.name for f in fields(Fact)]
# DB 物理列：基础 + v2/时效 + dim_key（dimensions 不入 DB）。upsert/_to_row 走它。
_DB_COLUMNS = _BASE_COLUMNS + _V2_COLUMNS + ["dim_key"]


def _compute_dim_key(fact: Fact) -> str:
    """从类型化身份列算确定性自然键（不读 dimensions 袋）。

    身份维度 = channel/entity/metric/period；geography 是 identity=False、不入键。
    period 用 period_type 前缀保证跨 FY/HY/Q 粒度单射
    （('FY','2024') 与 ('HY','2024') 得到不同 key）。sorted_keys 保证字节稳定。
    """
    return json.dumps(
        {
            "channel": str(fact.channel),
            "entity": str(fact.entity),
            "metric": str(fact.metric_code),
            "period": f"{fact.period_type}{fact.period}",
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class FactStore:
    """fact_metric 表的读写。维度组合唯一，重复入库走 upsert 覆盖。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # 确定性资源回收：对象被 GC 回收时（即便调用方忘了 close）也关连接，
        # 免得裸 sqlite 连接在 __del__ 阶段抛 ResourceWarning（被零警告门升级为失败）。
        self._finalizer = weakref.finalize(self, self._conn.close)

    def init_schema(self) -> None:
        """建表 + 唯一索引（同一指标×实体×期间×渠道只允许一条）+ v2 迁移补列。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fact_metric (
                metric_code   TEXT NOT NULL,
                entity        TEXT NOT NULL,
                geography     TEXT NOT NULL,
                channel       TEXT NOT NULL,
                period_type   TEXT NOT NULL,
                period        TEXT NOT NULL,
                value         REAL NOT NULL,
                unit          TEXT NOT NULL,
                source_doc_id TEXT NOT NULL,
                source_locator TEXT NOT NULL,
                dim_key        TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_metric
            ON fact_metric (metric_code, entity, period_type, period, channel)
            """
        )
        self._migrate_v2()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_dim_key "
            "ON fact_metric (dim_key)"
        )
        self._conn.commit()

    def _migrate_v2(self) -> None:
        """幂等地为既有表补上 v2 列与 dim_key 列（已有则跳过）；dim_key 现存
        NULL 行在同一事务内按该行身份列逐行回填（旧库平滑迁移、非破坏）。"""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(fact_metric)")}
        coldefs = {
            "tags": "TEXT NOT NULL DEFAULT '{}'",
            "source_file_hash": "TEXT",
            "extractor_version": "TEXT",
            "mapping_version": "INTEGER",
            "confidence": "REAL",
            "review_status": f"TEXT NOT NULL DEFAULT '{REVIEW_AUTO_APPROVED}'",
            "valid_as_of": "TEXT",
            "ingested_at": "TEXT",
            "corrected_by": "TEXT",
            "corrected_audit_seq": "INTEGER",
        }
        for col in _V2_COLUMNS:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE fact_metric ADD COLUMN {col} {coldefs[col]}")
        if "dim_key" not in existing:
            self._conn.execute("ALTER TABLE fact_metric ADD COLUMN dim_key TEXT")
        self._backfill_dim_key()

    def _backfill_dim_key(self) -> None:
        """对 dim_key IS NULL 的行，用该行身份列在 Python 算出 dim_key 并回填。"""
        rows = self._conn.execute(
            "SELECT rowid, metric_code, entity, channel, period_type, period "
            "FROM fact_metric WHERE dim_key IS NULL"
        ).fetchall()
        for row in rows:
            key = json.dumps(
                {
                    "channel": str(row["channel"]),
                    "entity": str(row["entity"]),
                    "metric": str(row["metric_code"]),
                    "period": f"{row['period_type']}{row['period']}",
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._conn.execute(
                "UPDATE fact_metric SET dim_key = ? WHERE rowid = ?", (key, row["rowid"])
            )

    def upsert_facts(self, facts: list[Fact], ingested_at: str | None = None) -> int:
        """批量写入；唯一键冲突时覆盖数值与血缘（含 v2 / 时效字段）。返回写入条数。

        ingested_at：本批入库时间戳（审计用，store 统一盖、调用方不能伪造）。
        缺省（None）时盖当下 UTC ISO；显式传入时以传入值为准（可测）。
        Fact 上即便自带 ingested_at 也被此戳覆盖（写入以 store 为准）。
        """
        stamp = ingested_at or datetime.now(UTC).isoformat()
        cols = ", ".join(_DB_COLUMNS)
        placeholders = ", ".join(["?"] * len(_DB_COLUMNS))
        update_cols = (
            "geography",
            "value",
            "unit",
            "source_doc_id",
            "source_locator",
            "tags",
            "source_file_hash",
            "extractor_version",
            "mapping_version",
            "confidence",
            "review_status",
            "valid_as_of",
            "ingested_at",
            "corrected_by",
            "corrected_audit_seq",
        )
        updates = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO fact_metric ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT (dim_key) "
            f"DO UPDATE SET {updates}"
        )
        rows = [self._to_row(f, ingested_at=stamp) for f in facts]
        self._conn.executemany(sql, rows)
        self._conn.commit()
        return len(rows)

    def query(
        self,
        metric_code: str,
        entity: str,
        period_type: str,
        period: str,
        channel: str = "TOTAL",
        review_statuses: tuple[str, ...] | None = VISIBLE_REVIEW_STATUSES,
    ) -> list[Fact]:
        """参数化精确查询，返回匹配的 Fact 列表（命中 0 或 1 条）。

        review_statuses：默认只返回可见状态（auto_approved / approved）；
        传 None 放开全部状态（用于审计 / 复核视图）。
        """
        sql = (
            "SELECT * FROM fact_metric "
            "WHERE metric_code = ? AND entity = ? AND period_type = ? "
            "AND period = ? AND channel = ?"
        )
        params: list[object] = [metric_code, entity, period_type, period, channel]
        if review_statuses is not None:
            marks = ", ".join(["?"] * len(review_statuses))
            sql += f" AND review_status IN ({marks})"
            params.extend(review_statuses)
        cur = self._conn.execute(sql, params)
        return [self._from_row(row) for row in cur.fetchall()]

    def count(self) -> int:
        """事实总条数。"""
        count: int = self._conn.execute(
            "SELECT COUNT(*) FROM fact_metric"
        ).fetchone()[0]
        return count

    def execute_read(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[sqlite3.Row]:
        """只读查询入口：跑参数化 SELECT 返回行列表（供台账/指标等观测面复用，
        免去外部直访私有连接）。"""
        return self._conn.execute(sql, params).fetchall()

    def delete_by_source_doc(self, source_doc_id: str) -> int:
        """按源文件一键撤下其全部事实（不论 review_status），返回删除条数。

        story #32：发现源文件有误时快速止血，物理删除而非隐藏。
        幂等：对不存在的 source_doc_id 调用返回 0、不报错。
        """
        cur = self._conn.execute(
            "DELETE FROM fact_metric WHERE source_doc_id = ?", (source_doc_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def set_review_status(self, dim_key: str, status: str) -> int:
        """按 dim_key 改写某条事实的 review_status，返回受影响行数（0 或 1）。

        人审写回用：approve→APPROVED 让事实可见，reject→REJECTED 让其不可见。
        dim_key 唯一，命中至多一行；不存在则返回 0、不报错。
        """
        cur = self._conn.execute(
            "UPDATE fact_metric SET review_status = ? WHERE dim_key = ?",
            (status, dim_key),
        )
        self._conn.commit()
        return cur.rowcount

    @staticmethod
    def dim_key_for(fact: "Fact") -> str:
        """从类型化身份列算该 Fact 的 dim_key（storage-only 自然键的公开取值口）。

        applier 需要 dim_key 才能 set_review_status / get_by_dim_key，但 dim_key
        绝不入 Fact 字段，故经此处统一从身份列重算（与 upsert 写入口同源）。
        """
        return _compute_dim_key(fact)

    def get_by_dim_key(self, dim_key: str) -> "Fact | None":
        """按 dim_key 取事实（不论 review_status），不存在返回 None。

        applier 据此读当前状态 / 校正血缘戳做幂等判断。
        """
        row = self._conn.execute(
            "SELECT * FROM fact_metric WHERE dim_key = ?", (dim_key,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def close(self) -> None:
        self._finalizer()  # 幂等：关连接并注销 finalizer，重复调用安全

    # --- 行 <-> Fact 序列化（tags 走 JSON）--------------------------------

    @staticmethod
    def _to_row(fact: Fact, ingested_at: str | None = None) -> tuple[object, ...]:
        values: list[object] = []
        for col in _DB_COLUMNS:
            if col == "dim_key":
                # storage-only：从类型化身份列重算，从不读 dimensions 袋。
                values.append(_compute_dim_key(fact))
                continue
            if col == "ingested_at":
                # store 盖的审计戳胜出（覆盖 Fact 自带值），写入以 store 为准。
                values.append(ingested_at)
                continue
            val = getattr(fact, col)
            if col == "tags":
                val = json.dumps(val or {}, ensure_ascii=False)
            values.append(val)
        return tuple(values)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> "Fact":
        # 只用 Fact 的持久化字段名（基础 + v2/时效/更正血缘）从 row 取值；
        # dim_key 是 storage-only（喂进 Fact 会 TypeError），dimensions 由
        # __post_init__ 重新派生——二者都不回灌进构造。
        data = {}
        keys = set(row.keys())
        for col in _BASE_COLUMNS + _V2_COLUMNS:
            if col not in keys:
                continue
            val = row[col]
            if col == "tags":
                val = json.loads(val) if val else {}
            data[col] = val
        return Fact(**data)
