"""TableStructureRecognizer 缝：已知表格区域 → 单元格网格（行 / 列 / 合并）。

**为什么只担这一件事**（2026-08 实测定位，FinTabNet.c 150 页 / 186 金标表）：

| pdfspine 策略 | 检出召回 | 检出精确率 | 仅在框对的表上 GriTS_Top |
|---|---|---|---|
| `strategy="lines"` | 21.5% | 20.6% | 0.340 |
| `strategy="text"`  | **79.6%** | **100%** | **0.233** |

表格【检出】已经够好——`text` 策略 148 个检出全部命中金标、零误报。崩掉的是检出**之后**的
单元格网格重建。故本缝刻意**不做表格检测**（那是调用方的事，现状已够用），只吃一个已知的
表格区域，吐出行列网格。

**文字内容绝不由本缝产出。** 数字 PDF 的文本层是精确的（pdfspine 文本抽取已与 PyMuPDF 打平），
让视觉模型去"读"字只会凭空引入错误。本缝只给坐标，调用方按格子坐标去文本层取字——这是
2026 年 SOTA（DELTA 等）的做法，也是本家族「结构与内容分离」的必然选择。

**五段式**（与家族其他缝同构，见 `graph/extractor.py` / `retrieval` 的 embedding 缝）：
Protocol（本文件）+ 离线确定性默认实现（`GridStructureRecognizer`，零三方依赖）+
`make_table_structure_recognizer` 工厂 + `RAGSPINE_TABLE_STRUCTURE` env 选型 +
参数化 conformance（`tests/conformance/test_table_structure.py`）。

**默认 None＝关**：不注入即完全不改变既有抽取路径，字节不变（同 `make_relation_extractor`
/ `make_narrative_graph` 的默认关纪律）。返回 `None` 的语义是「没有意见」，调用方沿用自己
原有的网格——绝不编造一个空网格冒充结果。
"""

import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "TABLE_STRUCTURE_ENV",
    "CellBox",
    "GridStructureRecognizer",
    "TableRegion",
    "TableStructure",
    "TableStructureRecognizer",
    "Word",
    "make_table_structure_recognizer",
]

# 选型环境变量（缺省 spec 时读取；范式同 RAGSPINE_RELATION_EXTRACTOR）。
TABLE_STRUCTURE_ENV = "RAGSPINE_TABLE_STRUCTURE"

# 词心聚类的默认容差（PDF point）。行容差取常见行距的一半量级，列容差取字距量级。
DEFAULT_ROW_TOLERANCE = 6.0
DEFAULT_COL_TOLERANCE = 12.0

Bbox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Word:
    """文本层的一个词及其 bbox（x0, y0, x1, y1，PDF point，y 向下增）。"""

    text: str
    bbox: Bbox

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


@dataclass(frozen=True)
class TableRegion:
    """一个【已知】的表格区域 + 其中的文本层词（本缝的输入）。

    `render` 是给视觉后端用的惰性栅格化钩子：`render(dpi) -> PNG bytes`。核心与默认实现
    都**不**调用它，故核心不依赖任何 PDF 库——保持 import-clean 与离线可测。
    """

    bbox: Bbox
    words: tuple[Word, ...]
    page_width: float
    page_height: float
    render: Any = None


@dataclass(frozen=True)
class CellBox:
    """网格中的一个单元格：逻辑坐标（row/col + 跨度）+ 物理 bbox。"""

    row: int
    col: int
    row_span: int
    col_span: int
    bbox: Bbox


@dataclass(frozen=True)
class TableStructure:
    """一张表的完整网格。`cells` 按 (row, col) 升序，保证确定性与可比较。"""

    n_rows: int
    n_cols: int
    cells: tuple[CellBox, ...]


@runtime_checkable
class TableStructureRecognizer(Protocol):
    """结构识别器缝的最小结构接口（核心只 import 本 Protocol，不 import 任何 ML SDK）。"""

    name: str

    def recognize(self, region: TableRegion) -> TableStructure | None:
        """返回网格；无法判断时返回 `None`（＝没有意见，调用方沿用原结构）。"""
        ...


def _cluster(values: list[float], tolerance: float) -> list[list[int]]:
    """把带索引的一维坐标按容差聚成升序的簇（确定性单遍扫描，无随机化）。"""
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    clusters: list[list[int]] = []
    current: list[int] = []
    anchor = 0.0
    for index in order:
        value = values[index]
        if not current or abs(value - anchor) <= tolerance:
            if not current:
                anchor = value
            current.append(index)
        else:
            clusters.append(current)
            current = [index]
            anchor = value
    if current:
        clusters.append(current)
    return clusters


class GridStructureRecognizer:
    """离线确定性默认实现：由词心位置推断行列网格，零三方依赖。

    行由词心 y 聚类得到，列由词心 x 聚类得到；每格 bbox 取该行该列交点处词的并集包围盒，
    空格退化为行带 × 列带的交集矩形。**不推断合并单元格**（`row_span`/`col_span` 恒为 1）——
    这是诚实的能力边界：跨格合并需要视觉证据，规则法猜不出，留给视觉后端。

    同输入同输出（聚类是排序后单遍扫描，簇与格子一律升序），故可逐位复现。
    """

    name = "grid"

    def __init__(
        self,
        *,
        row_tolerance: float = DEFAULT_ROW_TOLERANCE,
        col_tolerance: float = DEFAULT_COL_TOLERANCE,
    ) -> None:
        self.row_tolerance = row_tolerance
        self.col_tolerance = col_tolerance

    def recognize(self, region: TableRegion) -> TableStructure | None:
        words = region.words
        if not words:
            return None

        centers = [word.center for word in words]
        row_clusters = _cluster([c[1] for c in centers], self.row_tolerance)
        col_clusters = _cluster([c[0] for c in centers], self.col_tolerance)
        if not row_clusters or not col_clusters:
            return None

        row_of = {index: row for row, members in enumerate(row_clusters) for index in members}
        col_of = {index: col for col, members in enumerate(col_clusters) for index in members}

        # 每行 / 每列的物理带（用于空格退化 bbox）。
        row_bands = [
            (
                min(words[i].bbox[1] for i in members),
                max(words[i].bbox[3] for i in members),
            )
            for members in row_clusters
        ]
        col_bands = [
            (
                min(words[i].bbox[0] for i in members),
                max(words[i].bbox[2] for i in members),
            )
            for members in col_clusters
        ]

        occupied: dict[tuple[int, int], list[int]] = {}
        for index in range(len(words)):
            occupied.setdefault((row_of[index], col_of[index]), []).append(index)

        rx0, ry0, rx1, ry1 = region.bbox

        def clamp(bbox: Bbox) -> Bbox:
            x0, y0, x1, y1 = bbox
            return (
                min(max(x0, rx0), rx1),
                min(max(y0, ry0), ry1),
                min(max(x1, rx0), rx1),
                min(max(y1, ry0), ry1),
            )

        cells: list[CellBox] = []
        for row in range(len(row_clusters)):
            for col in range(len(col_clusters)):
                members = occupied.get((row, col))
                if members:
                    bbox = (
                        min(words[i].bbox[0] for i in members),
                        min(words[i].bbox[1] for i in members),
                        max(words[i].bbox[2] for i in members),
                        max(words[i].bbox[3] for i in members),
                    )
                else:
                    bbox = (
                        col_bands[col][0],
                        row_bands[row][0],
                        col_bands[col][1],
                        row_bands[row][1],
                    )
                cells.append(CellBox(row=row, col=col, row_span=1, col_span=1, bbox=clamp(bbox)))

        return TableStructure(
            n_rows=len(row_clusters), n_cols=len(col_clusters), cells=tuple(cells)
        )


def make_table_structure_recognizer(
    spec: str | None = None, **kwargs: Any
) -> TableStructureRecognizer | None:
    """结构识别器选型工厂：默认 None＝关（既有抽取路径字节不变）。

    spec 取值（大小写 / 留白 / 连字符不敏感；缺省读环境变量 `RAGSPINE_TABLE_STRUCTURE`）：
        - None / 'none'          -> None（默认关；调用方沿用自己原有的网格）
        - 'grid' / 'deterministic' -> GridStructureRecognizer（词心聚类，离线确定）
        - 'tatr'                 -> 视觉后端（需 `[tsr]` extra，延迟 import；缺 extra 友好报错）
        - 其他                   -> ValueError（列清可用 spec）
    """
    if spec is None:
        spec = os.environ.get(TABLE_STRUCTURE_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")

    if normalized in {"", "none", "off"}:
        return None
    if normalized in {"grid", "deterministic", "rule"}:
        return GridStructureRecognizer(**kwargs)
    if normalized in {"tatr", "vision"}:
        from ragspine.extraction.tables.adapters.tatr import TatrStructureRecognizer

        return TatrStructureRecognizer(**kwargs)
    raise ValueError(
        f"unknown table structure recognizer spec {spec!r}; expected one of: none / grid / tatr"
    )
