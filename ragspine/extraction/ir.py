"""样式感知网格中间表示（styled_grid IR）—— 全项目最稳定的接口。

抽取层（xlsx/pdf/pptx）统一产出 StyledGrid；下游（颜色语义、入库、复核、评测）
只消费它。每个单元格带值、解析后的真实颜色、合并范围、数字格式、精确血缘与来源
告警标记，理解层与数据源格式彻底解耦（PRD user story 1）。

实现已完成，dataclass 字段契约保持冻结，行为契约见 tests/extraction/test_ir.py。
"""

from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class StyledCell:
    """单个单元格的样式感知表示。

    字段语义约定：
        value:          单元格原始值（数值/文本/None）。
        cell_ref:       单元格坐标，如 'C4'（sheet 名记录在 StyledGrid 上）。
        resolved_rgb:   解析 theme + tint 后的填充色，'RRGGBB' 大写十六进制；
                        无填充或填充为 'none' 时为 None（user story 2）。
        number_format:  openpyxl 风格的数字格式串（如 '0.0%'、'#,##0'、'$#,##0.00'）；
                        无显式格式为 'General'（user story 4）。
        bold:           字体是否加粗。
        is_merged_origin: 该格是否为某合并区域的左上锚点（承载合并值的格）。
        merge_span:     若为合并锚点，记录跨度 (n_rows, n_cols)；非合并区为 None
                        （user story 3）。
        cf_affected:    该格是否落在条件格式规则区域内 —— 为 True 时其填充色来源
                        不可靠，颜色语义必须跳过（user story 5，PRD「条件格式不求值」）。
        confidence:     【OCR 通道专用】OCR/VLM 识别该格的置信度 [0.0, 1.0]；
                        确定性抽取（xlsx/pdf 数字型/pptx 原生）无此概念，恒为 None。
                        扫描线抽取器据此把低置信格分流进复核队列（user story 10）。

    行为约定：
        rgb_tag_key()   返回用于同色聚类的稳定键（cf_affected 的格不参与聚类）。
    """

    value: object
    cell_ref: str
    resolved_rgb: str | None = None
    number_format: str = "General"
    bold: bool = False
    is_merged_origin: bool = False
    merge_span: tuple[int, int] | None = None
    cf_affected: bool = False
    confidence: float | None = None

    def rgb_tag_key(self) -> str | None:
        """返回参与同色聚类的颜色键。

        约定：有可靠 resolved_rgb 且非 cf_affected 时返回该 RGB；否则返回 None
        （None 表示不参与聚类）。
        """
        if self.cf_affected:
            return None
        return self.resolved_rgb


@dataclass
class StyledGrid:
    """一张工作表 / 一页表格的样式感知网格。

    字段语义约定：
        sheet:            来源 sheet 名 / 页标识。
        cells:            cell_ref -> StyledCell 的稀疏映射（只存非空/有样式的格）。
        source_doc_id:    源文件名（血缘根，对应 fact_metric.source_doc_id）。
        source_file_hash: 源文件内容 hash（user story 18/27，版本与审计依据）。
        warnings:         grid 级告警（如「检测到条件格式区域 X」「转置表跳过」），
                          字符串列表，追加式。
        n_rows / n_cols:  网格逻辑行列数（1-based 上界），便于遍历定位。

    行为约定：
        get(cell_ref)            取某格；不存在返回 None。
        iter_cells()             遍历全部 StyledCell（顺序不保证，断言用集合/字典）。
        cells_by_rgb()           按 resolved_rgb 分组（跳过 None / cf_affected）。
        add_warning(msg)         追加 grid 级告警。
    """

    sheet: str
    source_doc_id: str
    source_file_hash: str | None = None
    cells: dict[str, StyledCell] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0

    def get(self, cell_ref: str) -> StyledCell | None:
        """按坐标取格；不存在返回 None。"""
        return self.cells.get(cell_ref)

    def iter_cells(self) -> Iterator[StyledCell]:
        """遍历网格内全部 StyledCell。"""
        return iter(self.cells.values())

    def cells_by_rgb(self) -> dict[str, list[StyledCell]]:
        """按 resolved_rgb 聚合可靠着色的格 -> {rgb: [cell, ...]}。

        跳过 resolved_rgb 为 None 或 cf_affected 的格。
        """
        grouped: dict[str, list[StyledCell]] = {}
        for cell in self.cells.values():
            key = cell.rgb_tag_key()
            if key is None:
                continue
            grouped.setdefault(key, []).append(cell)
        return grouped

    def add_warning(self, message: str) -> None:
        """追加一条 grid 级告警。"""
        self.warnings.append(message)
