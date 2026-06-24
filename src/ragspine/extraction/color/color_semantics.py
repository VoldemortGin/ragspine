"""颜色语义层（L2 受控推断）：同色聚类、图例识别、版本化映射注册表、映射应用。

架构红线（PRD「L2 受控语义推断」）：颜色→属性映射靠图例识别 + 聚类生成**草案**，
经 SME 确认后才进入版本化注册表；未确认映射绝不静默入库 —— apply 未确认映射时
相关 tags 置空并告警。注册表是独立持久化资产（sqlite），事实表通过 mapping_version
引用它。
"""

import json
import sqlite3
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openpyxl.utils import coordinate_to_tuple, get_column_letter

from ragspine.extraction.ir import StyledGrid


@dataclass
class ColorCluster:
    """一个同色簇：一种填充色 + 落在该色的单元格坐标。

    字段：
        rgb:        'RRGGBB' 大写十六进制。
        cell_refs:  属于该色的单元格坐标列表。
        count:      簇内格子数（= len(cell_refs)，便于报告排序）。
    """

    rgb: str
    cell_refs: list[str] = field(default_factory=list)
    count: int = 0


@dataclass
class LegendEntry:
    """图例区解析出的一条「颜色→含义」草案。

    字段：
        rgb:           色块格的真实 RGB。
        meaning:       图例文字所述含义（如 '新产品线'）。
        tag_key:       该含义归一后的 tag 键（如 'product_line'），用于 apply 时打 tag。
        tag_value:     tag 取值（如 'new'）。
        source_ref:    图例所在单元格坐标（血缘）。
    """

    rgb: str
    meaning: str
    tag_key: str | None = None
    tag_value: str | None = None
    source_ref: str | None = None


@dataclass
class ColorMapping:
    """一份颜色映射（注册表中的一条草案 / 生效项）。

    字段：
        scope:          适用范围（工作簿名 / 表模板标识）。
        entries:        LegendEntry 列表（rgb→tag 的具体条目）。
        version:        版本号，同 scope 下自增（user story 26：修订生成新版本不覆盖）。
        status:         'draft' / 'active' / 'rejected'。
        confirmed_by:   确认人（approve 时写入，留痕）。
        confirmed_at:   确认时间 ISO 串。
        note:           确认 / 驳回备注。
    """

    scope: str
    entries: list[LegendEntry] = field(default_factory=list)
    version: int = 0
    status: str = "draft"
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    note: str | None = None


# 图例文字含义关键词 → tag 归一映射（detect_legend 生成草案条目用）。
# 图例文字形如「黄色=新产品线」，取 '=' 右侧关键词。
_MEANING_TO_TAG: dict[str, tuple[str, str]] = {
    "新产品线": ("product_line", "new"),
    "成熟产品线": ("product_line", "mature"),
}


def cluster_colors(grid: StyledGrid) -> list[ColorCluster]:
    """对一张 grid 做同色聚类，生成颜色分组报告（user story 6）。

    只统计可靠着色的格（resolved_rgb 非空且非 cf_affected）；按簇大小降序返回
    （大小相同按 rgb 升序，使报告顺序稳定）。
    """
    grouped = grid.cells_by_rgb()  # 已跳过 resolved_rgb=None / cf_affected
    clusters = [
        ColorCluster(rgb=rgb, cell_refs=[c.cell_ref for c in cells], count=len(cells))
        for rgb, cells in grouped.items()
    ]
    clusters.sort(key=lambda c: (-c.count, c.rgb))
    return clusters


def _meaning_to_tag(meaning: str) -> tuple[str | None, str | None]:
    """从图例文字解析 tag_key / tag_value；'颜色=含义' 取 '=' 右侧关键词。"""
    keyword = meaning.split("=", 1)[1].strip() if "=" in meaning else meaning.strip()
    return _MEANING_TO_TAG.get(keyword, (None, None))


def detect_legend(grid: StyledGrid) -> list[LegendEntry]:
    """识别 sheet 内的图例区，生成「颜色→含义」映射草案（user story 7）。

    识别模式：相邻的「色块格 + 文字格」—— 色块格有可靠填充色且自身无值，其右侧紧邻格
    承载图例文字。每条草案带血缘（source_ref 指向图例文字格）。返回按文字格坐标排序的
    草案列表；识别不到返回空列表（不臆造）。
    """
    entries: list[LegendEntry] = []
    for cell in grid.iter_cells():
        rgb = cell.rgb_tag_key()  # 可靠填充色且非 cf_affected
        if rgb is None or cell.value is not None:
            continue
        row, col = coordinate_to_tuple(cell.cell_ref)
        text_ref = f"{get_column_letter(col + 1)}{row}"
        text_cell = grid.get(text_ref)
        if text_cell is None or not isinstance(text_cell.value, str) or not text_cell.value.strip():
            continue
        tag_key, tag_value = _meaning_to_tag(text_cell.value)
        entries.append(
            LegendEntry(
                rgb=rgb,
                meaning=text_cell.value,
                tag_key=tag_key,
                tag_value=tag_value,
                source_ref=text_ref,
            )
        )
    entries.sort(key=lambda e: coordinate_to_tuple(e.source_ref))
    return entries


def apply_mapping(grid: StyledGrid, mapping: ColorMapping) -> dict[str, dict[str, str]]:
    """把一份映射应用到 grid，返回 {cell_ref: {tag_key: tag_value}}。

    约定（PRD「未确认映射不静默入库」）：
        - mapping.status != 'active' 时返回空 tags，并向 grid.warnings 追加告警；
        - cf_affected / 无可靠填充色的格不打颜色 tag；
        - 只为命中映射条目 rgb 的格生成 tags。
    """
    if mapping.status != "active":
        grid.add_warning(
            f"颜色映射 scope={mapping.scope} status={mapping.status} 未确认，跳过打 tag"
        )
        return {}

    rgb_to_tag = {
        e.rgb: (e.tag_key, e.tag_value)
        for e in mapping.entries
        if e.tag_key is not None and e.tag_value is not None
    }

    result: dict[str, dict[str, str]] = {}
    for cell in grid.iter_cells():
        rgb = cell.rgb_tag_key()  # cf_affected / 无填充 -> None，不打 tag
        if rgb is None:
            continue
        tag = rgb_to_tag.get(rgb)
        if tag is None:
            continue
        tag_key, tag_value = tag
        result[cell.cell_ref] = {tag_key: tag_value}
    return result


class MappingRegistry:
    """颜色映射注册表（sqlite 持久化，与 fact_store 同库不同表）。

    生命周期：register_draft（落草案，version 自增）→ SME confirm / reject
    （留痕：谁、何时、备注）→ get_active 取当前生效版本。修订映射生成新版本而非覆盖
    （user story 26），事实表通过 mapping_version 引用历史依据。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # 确定性资源回收：对象被 GC 回收时（即便调用方忘了 close）也关连接，
        # 免得裸 sqlite 连接在 __del__ 阶段抛 ResourceWarning（被零警告门升级为失败）。
        self._finalizer = weakref.finalize(self, self._conn.close)

    def init_schema(self) -> None:
        """建映射注册表（color_mapping）+ 必要索引。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS color_mapping (
                scope        TEXT    NOT NULL,
                version      INTEGER NOT NULL,
                entries_json TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'draft',
                confirmed_by TEXT,
                confirmed_at TEXT,
                note         TEXT,
                created_at   TEXT    NOT NULL,
                PRIMARY KEY (scope, version)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_color_mapping_scope_status "
            "ON color_mapping (scope, status)"
        )
        self._conn.commit()

    def register_draft(self, mapping: ColorMapping) -> int:
        """登记一份草案映射（status='draft'），同 scope 下 version 自增（按 scope 隔离）。

        返回新草案的 version 号。
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS m FROM color_mapping WHERE scope = ?",
            (mapping.scope,),
        ).fetchone()
        version = int(row["m"]) + 1
        self._conn.execute(
            "INSERT INTO color_mapping "
            "(scope, version, entries_json, status, created_at) "
            "VALUES (?, ?, ?, 'draft', ?)",
            (mapping.scope, version, _dump_entries(mapping.entries), _now_iso()),
        )
        self._conn.commit()
        return version

    def confirm(self, scope: str, version: int, actor: str, note: str | None = None) -> None:
        """SME 确认某版本 -> status='active' 并写入 confirmed_by / confirmed_at / note。

        同 scope 下原 active 版本不被删除（历史可追溯），仅当前生效指针前移：旧 active
        退为 'superseded'，本版本升为 'active'。
        """
        self._conn.execute(
            "UPDATE color_mapping SET status = 'superseded' "
            "WHERE scope = ? AND status = 'active'",
            (scope,),
        )
        self._conn.execute(
            "UPDATE color_mapping "
            "SET status = 'active', confirmed_by = ?, confirmed_at = ?, note = ? "
            "WHERE scope = ? AND version = ?",
            (actor, _now_iso(), note, scope, version),
        )
        self._conn.commit()

    def reject(self, scope: str, version: int, actor: str, note: str | None = None) -> None:
        """驳回某版本 -> status='rejected' 并留痕（谁、何时、备注）。"""
        self._conn.execute(
            "UPDATE color_mapping "
            "SET status = 'rejected', confirmed_by = ?, confirmed_at = ?, note = ? "
            "WHERE scope = ? AND version = ?",
            (actor, _now_iso(), note, scope, version),
        )
        self._conn.commit()

    def execute_read(
        self, sql: str, params: tuple[object, ...] = ()
    ) -> list[sqlite3.Row]:
        """只读查询入口：跑参数化 SELECT 返回行列表（供版本清单等观测面复用，
        免去外部直访私有连接）。"""
        return self._conn.execute(sql, params).fetchall()

    def get_active(self, scope: str) -> ColorMapping | None:
        """取某 scope 当前生效（active）映射；无则返回 None。"""
        row = self._conn.execute(
            "SELECT scope, version, entries_json, status, confirmed_by, confirmed_at, note "
            "FROM color_mapping WHERE scope = ? AND status = 'active' "
            "ORDER BY version DESC LIMIT 1",
            (scope,),
        ).fetchone()
        if row is None:
            return None
        return ColorMapping(
            scope=row["scope"],
            entries=_load_entries(row["entries_json"]),
            version=int(row["version"]),
            status=row["status"],
            confirmed_by=row["confirmed_by"],
            confirmed_at=row["confirmed_at"],
            note=row["note"],
        )

    def close(self) -> None:
        self._finalizer()  # 幂等：关连接并注销 finalizer，重复调用安全


def _now_iso() -> str:
    """当前 UTC 时间 ISO 串（确认 / 驳回留痕用）。"""
    return datetime.now(UTC).isoformat()


def _dump_entries(entries: list[LegendEntry]) -> str:
    """LegendEntry 列表序列化为 JSON（注册表持久化）。"""
    return json.dumps(
        [
            {
                "rgb": e.rgb,
                "meaning": e.meaning,
                "tag_key": e.tag_key,
                "tag_value": e.tag_value,
                "source_ref": e.source_ref,
            }
            for e in entries
        ],
        ensure_ascii=False,
    )


def _load_entries(entries_json: str) -> list[LegendEntry]:
    """从 JSON 反序列化 LegendEntry 列表。"""
    return [
        LegendEntry(
            rgb=d["rgb"],
            meaning=d["meaning"],
            tag_key=d.get("tag_key"),
            tag_value=d.get("tag_value"),
            source_ref=d.get("source_ref"),
        )
        for d in json.loads(entries_json)
    ]
