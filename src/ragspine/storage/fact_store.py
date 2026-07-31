"""FactStore 缝：结构化指标事实存储（🛡 反编造 + provenance 不变量所在，live 契约见 src/ragspine/storage/CLAUDE.md）。

结构化通路是【反编造不变量】的存储侧根基：确定性抽取出的每个数值落成一条 Fact，带完整数据血缘
（源文件 + 定位串）。查询走参数化精确匹配，**命中即确定值、未命中即空**（绝不插值/臆造/漂移），
agent 侧据此在无 found fact 时把答案改写为「未找到」。这条缝把这套语义形式化为一个【可注册 /
可选择 / 带 conformance pack】的 Protocol——五段式范式同 make_vector_store / make_graph_store /
make_trace_sink：

    1. Protocol —— @runtime_checkable FactStore，抽出 sqlite 默认实现的公开接口（query / upsert_facts
       / delete_by_source_doc / found→值·miss→空 / provenance round-trip），core 只 import 这个 Protocol。
    2. 离线默认 —— SqliteFactStore（stdlib sqlite3，零三方依赖、确定性），行为逐位不变，让
       `pip install ragspine` 的精简默认仍可端到端跑结构化通路（ADR 0005/0009）。
    3. 薄 adapter —— DuckDB / Postgres 为 follow-up（需外部依赖，behind 各自 extra、延迟 import；
       第三方今天即可经 entry-point group 注册，无需核心 PR）。
    4. 注册表 —— make_fact_store / RAGSPINE_FACT_STORE，内置 sqlite 默认 + entry-point 自动发现
       （group ragspine.fact_stores）；缺省 spec → sqlite 默认实现（默认 loop 字节不变）。
    5. conformance —— tests/conformance/test_fact_store.py 对【每个注册实现】参数化断言反编造 +
       provenance 不变量（found-determinism / miss→空 / lineage 存活），伪造/丢血缘的 stub 直接 CI 红。

两项不变量如何被【实现真正保证】（非注释）：
- 反编造 —— query() 参数化精确匹配，命中即返回确定值（跨调用逐位一致）、未命中即空列表；
  dim_key 唯一保证命中 0-或-1 行，这是「found→值·miss→空」确定性读路径的根基。
- Provenance —— Fact 的 source_doc_id / source_locator 原样存、原样随 query 回传；upsert/query 全程
  不丢血缘，人工更正另加 corrected_by / corrected_audit_seq，故 found 结果永远可回指来源。

v2（多模态抽取期）：Fact 增加可选的样式语义与版本血缘字段
（tags / source_file_hash / extractor_version / mapping_version / confidence /
review_status），schema 自动迁移补列；query() 默认只返回 review_status 可见的数据。
所有新字段都有默认值，旧调用（不传新字段）行为不变。
"""

import json
import os
import sqlite3
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 VECTOR_STORE_ENV / GRAPH_STORE_ENV）。
FACT_STORE_ENV = "RAGSPINE_FACT_STORE"

# 第三方后端自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.fact_stores"]`），make_fact_store 就能按名字选中它——
# 核心零改动、零 SDK import（范式同 VECTOR_STORE_ENTRY_POINT_GROUP / GRAPH_STORE_ENTRY_POINT_GROUP）。
FACT_STORE_ENTRY_POINT_GROUP = "ragspine.fact_stores"

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

    @classmethod
    def metric(
        cls,
        *,
        metric_code: str,
        entity: str,
        period_type: str,
        period: str,
        value: float,
        unit: str,
        source_doc_id: str,
        source_locator: str,
        channel: str = "TOTAL",
        geography: str = "",
        **extra: object,
    ) -> "Fact":
        """Keyword-only 建造器：免受 10 个位置字段顺序之扰，只按名字传。

        位置构造器 `Fact(metric_code, entity, geography, channel, period_type, period,
        value, unit, source_doc_id, source_locator, ...)` 的前十个字段是顺序敏感的、易错位
        （`qa_eval` 还靠 `Fact(*row)` 绑 10-tuple，故字段顺序不可改）。本建造器全部
        keyword-only、顺序无关：`channel` 缺省 'TOTAL'（同 `query` 口径），`geography`
        缺省 ''（无地理维，同 narrative_ingest 默认），其余 v2 可选字段
        （`tags` / `source_file_hash` / `confidence` / `review_status` / `valid_as_of` /
        `dimensions` ...）经 `**extra` 透传给位置构造器。

        例：
            fact = Fact.metric(
                metric_code="REVENUE", entity="ACME_CN",
                period_type="FY", period="2024",
                value=2680.0, unit="US$m",
                source_doc_id="q4.xlsx", source_locator="sheet=PL!B2",
                geography="CN", confidence=0.98,
            )
        """
        return cls(
            metric_code=metric_code,
            entity=entity,
            geography=geography,
            channel=channel,
            period_type=period_type,
            period=period,
            value=value,
            unit=unit,
            source_doc_id=source_doc_id,
            source_locator=source_locator,
            **extra,  # type: ignore[arg-type]
        )


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


@runtime_checkable
class FactStore(Protocol):
    """结构化事实存储缝的最小结构接口（core 只 import 这个 Protocol，不 import 任何 DB SDK）。

    刻意抽出【现有 sqlite 默认实现的公开接口】（不加不删方法、不改契约）——这套公开面正是反编造 +
    provenance 两项不变量绑定的地方：query 的 found→值·miss→空 三态、upsert 的血缘保全、按源撤下、
    dim_key 自然键、人审写回。任何实现（sqlite 默认现在，将来的 DuckDB / Postgres adapter）只要
    结构匹配本 Protocol 并通过 tests/conformance/test_fact_store.py，即可被 make_fact_store 选中。

    诚实边界（🛡 契约完整 > 抽象洁癖）：execute_read 是【sqlite 原生只读逃生口】（返回 sqlite3.Row，
    供台账/指标等观测面复用），刻意保留在公开面以匹配既有契约、保证调用方字节不变；一个非 sqlite
    后端要么回传兼容的 row-like、要么让这类观测面直接依赖具体实现——这是 DuckDB/Postgres adapter 的
    follow-up 关切，不在本缝的反编造/provenance 核心内。
    """

    def init_schema(self) -> None: ...

    def upsert_facts(self, facts: list[Fact], ingested_at: str | None = None) -> int: ...

    def query(
        self,
        metric_code: str,
        entity: str,
        period_type: str,
        period: str,
        channel: str = "TOTAL",
        review_statuses: tuple[str, ...] | None = VISIBLE_REVIEW_STATUSES,
    ) -> list[Fact]: ...

    def count(self) -> int: ...

    def execute_read(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[sqlite3.Row]: ...

    def delete_by_source_doc(self, source_doc_id: str) -> int: ...

    def set_review_status(self, dim_key: str, status: str) -> int: ...

    @staticmethod
    def dim_key_for(fact: "Fact") -> str: ...

    def get_by_dim_key(self, dim_key: str) -> "Fact | None": ...

    def close(self) -> None: ...


class SqliteFactStore:
    """fact_metric 表的读写（FactStore 缝的零依赖 sqlite 默认实现）。维度组合唯一，重复入库走 upsert 覆盖。

    行为逐位不变（原 concrete `FactStore` 改名而来，纯结构性提取，不改任何存储格式 / 查询语义 /
    dim_key / found-not-found 语义）。结构匹配 @runtime_checkable FactStore Protocol。

    线程契约（并发使用约定）——**一个实例绑定到创建它的线程，不跨线程共享**：
        - 底层是单条 `sqlite3.connect(...)`（默认 `check_same_thread=True`），故在【创建它的
          线程之外】使用同一实例会抛
          `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
          这是【刻意】的护栏，不是缺陷：共享一条 sqlite 连接跨线程写没有加锁保护，会静默损坏。
        - **FastAPI / 线程池场景**：不要把一个 store 实例挂到 app.state 跨请求共享。每个请求 / 每次
          操作【各自打开一个】store（服务层已如此：`service` 的 `open_fact_store(config)` 是
          per-request 上下文管理器，在承接该请求的线程里 open→用→close，天然满足本契约）。
          worker 侧同理，job 自持自己的 store。
        - 需要真正的并发读写？那是连接池 / WAL 多连接的活，属显式 follow-up（DuckDB/Postgres
          adapter 或 sqlite WAL 池），**本默认实现刻意不引入连接池**——保持零依赖、单连接、可预测。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # 单连接、绑定创建线程（check_same_thread 默认 True）。跨线程共享请改为 per-request/op
        # 各开一个 store（见类 docstring「线程契约」；服务层 open_fact_store 已照此办）。
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

    def has_source_doc(self, source_doc_id: str) -> bool:
        """该 source_doc_id 是否已有【任一】事实入库（不论 review_status）。

        存在性助手：产品层据此判断某源文档是否已 ingest（幂等入库 / 去重 / 增量刷新前的
        探测），免得 `execute_read('SELECT ...')` 再数长度。`LIMIT 1` 命中即返回，不拉全行。
        不存在返回 False、不报错。
        """
        row = self._conn.execute(
            "SELECT 1 FROM fact_metric WHERE source_doc_id = ? LIMIT 1",
            (source_doc_id,),
        ).fetchone()
        return row is not None

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


# ---------------------------------------------------------------------------
# 注册表：内置后端名字 -> 惰性 loader（返回 FactStore 实现【类】，尚不实例化）。范式同 make_graph_store：
# loader 仅在被调用时才 import 对应实现（本模块的 SqliteFactStore 零三方依赖；将来的 DuckDB / Postgres
# adapter 由各自 loader 延迟 import，behind 各自 extra）。第三方后端【不】登记此表，而是经 entry-point
# 自动发现（见 _discover_entry_points），无需任何核心 PR。别名共指同一 loader（大小写 / 留白由
# make_fact_store 归一化）。
# ---------------------------------------------------------------------------
def _load_sqlite() -> type[FactStore]:
    return SqliteFactStore


_BUILTIN_LOADERS: dict[str, Callable[[], type[FactStore]]] = {
    "sqlite": _load_sqlite,
    "sqlite3": _load_sqlite,
    "default": _load_sqlite,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "sqlite")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 FACT_STORE_ENTRY_POINT_GROUP 下注册的 FactStore 后端（范式同 vector / graph store）。

    在函数内 import entry_points，使 monkeypatch importlib.metadata.entry_points 在测试中生效，
    也让发现成本只在真正回落时付出。
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=FACT_STORE_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Callable[..., FactStore]:
    """归一化名字 -> 可 **kwargs 调用得到 FactStore 的工厂（内置类或 entry-point 目标）。

    内置优先于同名 entry point（第三方不能劫持内置 sqlite 默认语义）；未命中再回落 entry-point 自动
    发现。只【解析】不【实例化】——对内置默认不触发任何重依赖 import（sqlite 是 stdlib）。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            factory: Callable[..., FactStore] = entry_point.load()
            return factory
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 fact store spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {FACT_STORE_ENTRY_POINT_GROUP!r} 下注册一个后端）"
    )


def make_fact_store(spec: str | None = None, **kwargs: Any) -> FactStore | None:
    """FactStore 工厂：把「选哪个结构化后端」从改代码降为一个 spec/env（范式同 make_graph_store）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_FACT_STORE）：
        - None / 缺省 / 'sqlite' / 'sqlite3' / 'default' -> SqliteFactStore（零依赖 stdlib sqlite 默认实现，
          需传 db_path=...）。**缺省 spec → sqlite 默认实现**——默认结构化通路字节不变。
        - 'none'                                         -> None（显式不注入具体 store，供调用方自建）。
        - 其余                                            -> entry-point 自动发现（第三方包在
          FACT_STORE_ENTRY_POINT_GROUP 下注册 DuckDB / Postgres 等即可被选中）；都不命中 -> ValueError。

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化（SqliteFactStore 需 db_path）。
    返回 FactStore 实例或 None。DuckDB / Postgres 一方 adapter 为 follow-up（需外部依赖，不硬引入 CI）。
    """
    if spec is None:
        spec = os.environ.get(FACT_STORE_ENV)
    normalized = (spec or "sqlite").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    return factory(**kwargs)


__all__ = [
    "FACT_STORE_ENTRY_POINT_GROUP",
    "FACT_STORE_ENV",
    "REVIEW_APPROVED",
    "REVIEW_AUTO_APPROVED",
    "REVIEW_BLOCKED",
    "REVIEW_PENDING",
    "REVIEW_REJECTED",
    "VISIBLE_REVIEW_STATUSES",
    "Fact",
    "FactStore",
    "SqliteFactStore",
    "make_fact_store",
]
