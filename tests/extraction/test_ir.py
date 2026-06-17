"""样式感知网格中间表示（src/ir.py）的红色测试。

只验证 StyledCell / StyledGrid 的外部行为：取格、遍历、合并归属解析、
同色聚类键、按色聚合、告警聚合。每个用例都会触发某个尚未实现的行为方法
（raise NotImplementedError），故此阶段全部 FAIL 即为预期的"红"。

对应 PRD user stories：
  #1 统一样式感知网格中间表示（下游与数据源解耦）
  #2 theme+tint 解析后的真实 RGB 参与颜色聚类
  #3 合并单元格还原为多级表头语义
  #4 数字格式保留
  #5 条件格式区域打标、颜色语义跳过
  #6 同色单元格聚类
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.ir import StyledCell, StyledGrid


# --------------------------------------------------------------------------- #
# 构造小工具：基于 ground truth 行造一个 StyledCell（dataclass 构造本身不报错）
# --------------------------------------------------------------------------- #
def _cell(cell_ref, **kw) -> StyledCell:
    return StyledCell(value=kw.pop("value", None), cell_ref=cell_ref, **kw)


def _grid_from_truth(ground_truth, sheet, **grid_kw) -> StyledGrid:
    """把某张 sheet 的逐格真值灌进一个 StyledGrid（仅填字段，不触发行为方法）。"""
    grid = StyledGrid(sheet=sheet, source_doc_id=ground_truth["file"], **grid_kw)
    for row in ground_truth["cells"]:
        if row["sheet"] != sheet:
            continue
        grid.cells[row["cell_ref"]] = StyledCell(
            value=row["value"],
            cell_ref=row["cell_ref"],
            resolved_rgb=row["resolved_rgb"],
            number_format=row["number_format"],
            is_merged_origin=row["is_merged_origin"],
            merge_span=tuple(row["merge_span"]) if row["merge_span"] else None,
            cf_affected=row["cf_affected"],
        )
    return grid


# --------------------------------------------------------------------------- #
# StyledCell.rgb_tag_key —— 同色聚类键（story #2 / #5 / #6）
# --------------------------------------------------------------------------- #
def test_rgb_tag_key_returns_resolved_rgb_for_reliable_fill():
    """story #6 —— 有可靠 resolved_rgb 且非 cf 的格，聚类键即该 RGB。"""
    cell = _cell("B2", value=2100.0, resolved_rgb="FFFF00")
    assert cell.rgb_tag_key() == "FFFF00"


def test_rgb_tag_key_for_theme_resolved_rgb():
    """story #2 —— theme accent1+tint 解析出的 95B3D7 同样直接作为聚类键。"""
    cell = _cell("B5", value=0.125, resolved_rgb="95B3D7", number_format="0.0%")
    assert cell.rgb_tag_key() == "95B3D7"


def test_rgb_tag_key_none_when_no_fill():
    """story #6 —— 无填充（resolved_rgb=None）的格不参与聚类，键为 None。"""
    cell = _cell("A2", value="REVENUE", resolved_rgb=None)
    assert cell.rgb_tag_key() is None


def test_rgb_tag_key_none_when_cf_affected():
    """story #5 —— 条件格式区域内的格颜色来源不可靠，即使有色也不参与聚类。"""
    cell = _cell("B2", value=2680.0, resolved_rgb="FF0000", cf_affected=True)
    assert cell.rgb_tag_key() is None


def test_rgb_tag_key_cf_affected_without_color_is_none():
    """story #5 —— CF 受影响且 resolved_rgb 已被置空的格，键仍为 None。"""
    cell = _cell("B5", value=0.138, resolved_rgb=None, cf_affected=True)
    assert cell.rgb_tag_key() is None


# --------------------------------------------------------------------------- #
# StyledGrid.get —— 按坐标取格（story #1）
# --------------------------------------------------------------------------- #
def test_get_returns_cell_by_ref(ground_truth):
    """story #1 —— 按坐标取到对应 StyledCell（值与格式随之取回）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    cell = grid.get("B2")
    assert cell is not None
    assert cell.value == 2100.0
    assert cell.resolved_rgb == "FFFF00"
    assert cell.number_format == "#,##0"


def test_get_missing_ref_returns_none(ground_truth):
    """story #1 —— 取一个不存在的坐标返回 None（稀疏网格，空格不存）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    assert grid.get("Z99") is None


def test_get_on_empty_grid_returns_none():
    """story #1 —— 空网格取任何坐标都返回 None。"""
    grid = StyledGrid(sheet="Empty", source_doc_id="excel_styled_fixture.xlsx")
    assert grid.get("A1") is None


def test_get_preserves_number_format(ground_truth):
    """story #4 —— 取回的格保留原数字格式（货币串原样保留，不被吞）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    cell = grid.get("B4")
    assert cell is not None
    assert cell.number_format == '"$"#,##0.00'


# --------------------------------------------------------------------------- #
# StyledGrid.iter_cells —— 遍历全部格（story #1）
# --------------------------------------------------------------------------- #
def test_iter_cells_yields_all_cells(ground_truth):
    """story #1 —— 遍历产出网格内全部格（顺序不保证，按集合断言）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    refs = {c.cell_ref for c in grid.iter_cells()}
    expected = {
        "B1", "C1", "D1", "A1", "A2", "B2", "C2", "D2",
        "A3", "B3", "C3", "D3", "A4", "B4", "C4", "D4",
        "A5", "B5", "C5", "D5", "F2", "G2", "F3", "G3",
    }
    assert refs == expected


def test_iter_cells_empty_grid_yields_nothing():
    """story #1 —— 空网格遍历产出空集合。"""
    grid = StyledGrid(sheet="Empty", source_doc_id="excel_styled_fixture.xlsx")
    assert list(grid.iter_cells()) == []


def test_iter_cells_returns_styled_cell_instances(ground_truth):
    """story #1 —— 遍历产出的是 StyledCell 实例（携带样式而非裸值）。"""
    grid = _grid_from_truth(ground_truth, "Transposed")
    cells = list(grid.iter_cells())
    assert cells
    assert all(isinstance(c, StyledCell) for c in cells)


# --------------------------------------------------------------------------- #
# StyledGrid.cells_by_rgb —— 按真实色聚合（story #2 / #5 / #6）
# --------------------------------------------------------------------------- #
def test_cells_by_rgb_groups_same_color(ground_truth):
    """story #6 —— 同色格归到同一 RGB 分组（黄色 4 格、绿色 7 格）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    grouped = grid.cells_by_rgb()
    yellow = {c.cell_ref for c in grouped["FFFF00"]}
    green = {c.cell_ref for c in grouped["92D050"]}
    assert yellow == {"B2", "C2", "D2", "F2"}
    assert green == {"B3", "B4", "C3", "C4", "D3", "D4", "F3"}


def test_cells_by_rgb_includes_theme_resolved_color(ground_truth):
    """story #2 —— theme+tint 解析出的 95B3D7 作为独立分组出现（含 3 格）。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    grouped = grid.cells_by_rgb()
    assert {c.cell_ref for c in grouped["95B3D7"]} == {"B5", "C5", "D5"}


def test_cells_by_rgb_skips_uncolored(ground_truth):
    """story #6 —— 无填充的格（resolved_rgb=None）不进任何分组。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    grouped = grid.cells_by_rgb()
    grouped_refs = {c.cell_ref for cells in grouped.values() for c in cells}
    assert None not in grouped  # 不能出现 None 作为分组键
    assert "A2" not in grouped_refs  # 标签格无填充，必被跳过


def test_cells_by_rgb_skips_cf_affected(ground_truth):
    """story #5 —— 条件格式受影响区域不进颜色分组（来源不可靠，宁漏不错）。"""
    grid = _grid_from_truth(ground_truth, "CondFormat")
    grouped = grid.cells_by_rgb()
    cf_refs = {"B2", "B3", "B4", "B5"}
    grouped_refs = {c.cell_ref for cells in grouped.values() for c in cells}
    assert cf_refs.isdisjoint(grouped_refs)


def test_cells_by_rgb_empty_when_no_colors(ground_truth):
    """story #6 —— 全无可靠着色的网格（转置表）聚合结果为空。"""
    grid = _grid_from_truth(ground_truth, "Transposed")
    assert grid.cells_by_rgb() == {}


# --------------------------------------------------------------------------- #
# 合并单元格归属（story #3）—— 锚点承载值与跨度，靠 get + 遍历对外暴露
# --------------------------------------------------------------------------- #
def test_merge_origin_cell_carries_span(ground_truth):
    """story #3 —— 合并锚点 A1:F1 取回时带 is_merged_origin 与 (1,6) 跨度。"""
    grid = _grid_from_truth(ground_truth, "MergedHeader")
    cell = grid.get("A1")
    assert cell is not None
    assert cell.is_merged_origin is True
    assert cell.merge_span == (1, 3 + 3)
    assert cell.value == "ACME Hong Kong"


def test_multilevel_merge_origins_visible_via_iter(ground_truth):
    """story #3 —— 三级合并表头的全部锚点可经遍历枚举（A1 / A2 / D2）。"""
    grid = _grid_from_truth(ground_truth, "MergedHeader")
    origins = {c.cell_ref: c.merge_span for c in grid.iter_cells() if c.is_merged_origin}
    assert origins == {"A1": (1, 6), "A2": (1, 3), "D2": (1, 3)}


def test_non_merged_cell_has_no_span(ground_truth):
    """story #3 —— 合并区下方的普通数据格不带合并跨度（不会张冠李戴）。"""
    grid = _grid_from_truth(ground_truth, "MergedHeader")
    cell = grid.get("A4")
    assert cell is not None
    assert cell.is_merged_origin is False
    assert cell.merge_span is None


# --------------------------------------------------------------------------- #
# StyledGrid.add_warning —— grid 级告警聚合（story #5 / 转置告警 #14 兜底）
# --------------------------------------------------------------------------- #
def test_add_warning_appends(ground_truth):
    """story #5 —— 追加一条 grid 级告警后，warnings 列表可见该条。"""
    grid = _grid_from_truth(ground_truth, "CondFormat")
    grid.add_warning("检测到条件格式区域 B2:B5")
    assert "检测到条件格式区域 B2:B5" in grid.warnings


def test_add_warning_is_append_only(ground_truth):
    """story #5 —— 多次告警按追加顺序累积，不覆盖既有告警。"""
    grid = _grid_from_truth(ground_truth, "CondFormat")
    grid.add_warning("第一条")
    grid.add_warning("第二条")
    assert grid.warnings[-2:] == ["第一条", "第二条"]


def test_add_warning_preserves_preexisting(ground_truth):
    """story #5 —— 构造时已带告警的网格，再 add 不丢原有告警。"""
    grid = _grid_from_truth(ground_truth, "Transposed", warnings=["转置表跳过"])
    grid.add_warning("追加项")
    assert grid.warnings == ["转置表跳过", "追加项"]


# --------------------------------------------------------------------------- #
# 跨方法一致性 / 真值往返（story #1）
# --------------------------------------------------------------------------- #
def test_get_and_iter_agree(ground_truth):
    """story #1 —— get 取到的每个格都能在 iter_cells 里出现且为同一对象身份。"""
    grid = _grid_from_truth(ground_truth, "HK_Performance")
    by_iter = {c.cell_ref: c for c in grid.iter_cells()}
    cell = grid.get("C3")
    assert cell is by_iter["C3"]


def test_lineage_fields_round_trip(ground_truth):
    """story #1 —— 血缘字段（source_doc_id/hash）随网格携带，遍历不丢血缘。"""
    grid = _grid_from_truth(
        ground_truth, "HK_Performance", source_file_hash="deadbeef", n_rows=5, n_cols=7
    )
    # 触发行为方法以保持红：确认遍历可用的同时血缘字段在位
    cells = list(grid.iter_cells())
    assert cells
    assert grid.source_doc_id == "excel_styled_fixture.xlsx"
    assert grid.source_file_hash == "deadbeef"
