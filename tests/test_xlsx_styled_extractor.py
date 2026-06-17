"""XLSX 样式感知抽取器测试（TDD 红色阶段）。

只验证外部行为：给定合成 fixture（excel_styled_fixture.xlsx）与逐格 ground truth，
断言 extract_grids / resolve_theme_color / compute_file_hash 的对外输出。

覆盖 PRD user stories：
    #1  所有抽取器输出统一样式感知网格中间表示（每 sheet 一张 StyledGrid）。
    #2  theme 色 + tint 解析为真实 RGB。
    #3  合并单元格还原为多级表头语义（is_merged_origin + merge_span）。
    #4  数字格式（百分比/千分位/货币）保留。
    #5  条件格式被检测 -> cf_affected + grid 告警，而不是当静态颜色。
    #14 转置表等不规则表头至少被完整读出网格（值不丢）。
    #18/#27 source_file_hash 写入每张 grid，作为版本/审计血缘。

红色预期：所有用例因 stub raise NotImplementedError 而 FAIL（收集成功、无 PASS）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.extractors.xlsx_styled_extractor import (
    compute_file_hash,
    extract_grids,
    resolve_theme_color,
)


# ---------------------------------------------------------------------------
# 辅助：把抽取出的 grid 列表按 sheet 名索引
# ---------------------------------------------------------------------------

def _grids_by_sheet(path) -> dict:
    return {g.sheet: g for g in extract_grids(path)}


# ===========================================================================
# story #2 — theme 色 + tint -> 真实 RGB（纯函数）
# ===========================================================================

def test_resolve_theme_color_accent1_tint(ground_truth):
    """story #2 —— accent1(index 4) + tint 0.4 必须解析为 '95B3D7'。"""
    palette = ground_truth["theme_palette"]
    theme_rgbs = ["FFFFFF"] * 6
    theme_rgbs[palette["accent1_index"]] = palette["accent1_base_rgb"]
    out = resolve_theme_color(
        palette["accent1_index"], palette["accent1_tint"], theme_rgbs
    )
    assert out == palette["accent1_resolved_rgb"]


def test_resolve_theme_color_zero_tint_is_base(ground_truth):
    """story #2 —— tint=0 时应原样返回大写 base RGB（不调亮也不调暗）。"""
    palette = ground_truth["theme_palette"]
    theme_rgbs = ["000000"] * 6
    theme_rgbs[palette["accent1_index"]] = palette["accent1_base_rgb"]
    out = resolve_theme_color(palette["accent1_index"], 0.0, theme_rgbs)
    assert out == palette["accent1_base_rgb"].upper()


def test_resolve_theme_color_returns_uppercase_hex():
    """story #2 —— 返回值是 6 位大写十六进制 'RRGGBB'。"""
    out = resolve_theme_color(0, 0.0, ["4F81BD"])
    assert isinstance(out, str)
    assert len(out) == 6
    assert out == out.upper()
    int(out, 16)  # 必须是合法十六进制


# ===========================================================================
# story #18 / #27 — source_file_hash 血缘
# ===========================================================================

def test_compute_file_hash_is_hex_string(excel_fixture_path):
    """story #18 —— 文件 hash 是非空十六进制串，可作版本血缘标识。"""
    h = compute_file_hash(excel_fixture_path)
    assert isinstance(h, str)
    assert len(h) > 0
    int(h, 16)  # 十六进制


def test_compute_file_hash_is_deterministic(excel_fixture_path):
    """story #18 —— 同一文件两次计算 hash 必须一致（确定性）。"""
    assert compute_file_hash(excel_fixture_path) == compute_file_hash(excel_fixture_path)


def test_grids_carry_source_hash(excel_fixture_path):
    """story #27 —— 每张 grid 的 source_file_hash 等于该文件的 compute_file_hash。"""
    expected = compute_file_hash(excel_fixture_path)
    grids = extract_grids(excel_fixture_path)
    assert grids
    for g in grids:
        assert g.source_file_hash == expected


def test_grids_carry_source_doc_id(excel_fixture_path):
    """story #1 —— source_doc_id 为源文件名，作为下游血缘根。"""
    for g in extract_grids(excel_fixture_path):
        assert g.source_doc_id == excel_fixture_path.name


# ===========================================================================
# story #1 — 统一中间表示：每 sheet 一张 StyledGrid
# ===========================================================================

def test_extract_one_grid_per_sheet(excel_fixture_path):
    """story #1 —— fixture 4 个 sheet 各产出一张 StyledGrid。"""
    grids = extract_grids(excel_fixture_path)
    assert {g.sheet for g in grids} == {
        "HK_Performance",
        "MergedHeader",
        "Transposed",
        "CondFormat",
    }


def test_grid_get_and_iter_consistent(excel_fixture_path):
    """story #1 —— grid.get / iter_cells 返回 StyledCell，且 cell_ref 自洽。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    cell = grid.get("B2")
    assert cell is not None
    assert cell.cell_ref == "B2"
    refs = {c.cell_ref for c in grid.iter_cells()}
    assert "B2" in refs


def test_grid_missing_cell_returns_none(excel_fixture_path):
    """story #1 —— 取一个不存在/空白的格返回 None（稀疏映射）。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    assert grid.get("Z99") is None


# ===========================================================================
# story #4 + 普通 RGB —— 值/普通填充色/数字格式全部保留
# ===========================================================================

def test_plain_rgb_value_and_number_format(cell_truth, excel_fixture_path):
    """story #4 —— 千分位格 B2：值 2100、黄色 FFFF00、格式 '#,##0' 全保留。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    truth = cell_truth["HK_Performance!B2"]
    cell = grid.get("B2")
    assert cell.value == truth["value"]
    assert cell.resolved_rgb == truth["resolved_rgb"]  # FFFF00
    assert cell.number_format == truth["number_format"]  # #,##0


def test_percent_format_preserved(cell_truth, excel_fixture_path):
    """story #4 —— 百分比格 B5：原始值 0.125 与格式 '0.0%' 都保留（不与 14% 混淆）。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    truth = cell_truth["HK_Performance!B5"]
    cell = grid.get("B5")
    assert cell.value == truth["value"]  # 0.125 原始小数
    assert cell.number_format == "0.0%"


def test_currency_format_preserved(cell_truth, excel_fixture_path):
    """story #4 —— 货币格 B4：货币数字格式串被原样保留。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    truth = cell_truth["HK_Performance!B4"]
    cell = grid.get("B4")
    assert cell.value == truth["value"]
    assert cell.number_format == truth["number_format"]  # "$"#,##0.00


def test_grey_header_plain_rgb(cell_truth, excel_fixture_path):
    """story #2 —— 普通 RGB 表头底色 D9D9D9 被原样解析。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    cell = grid.get("B1")
    assert cell.resolved_rgb == cell_truth["HK_Performance!B1"]["resolved_rgb"]


def test_unfilled_cell_rgb_is_none(cell_truth, excel_fixture_path):
    """story #2 —— 无填充的指标标签格 A2 resolved_rgb 为 None。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    cell = grid.get("A2")
    assert cell.resolved_rgb is None


# ===========================================================================
# story #2 —— theme + tint 填充经抽取后落为真实 RGB
# ===========================================================================

def test_theme_tint_fill_resolved_in_grid(cell_truth, excel_fixture_path):
    """story #2 —— ROE 行 theme accent1+tint 填充抽取后 resolved_rgb == '95B3D7'。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    for ref in ("B5", "C5", "D5"):
        cell = grid.get(ref)
        assert cell.resolved_rgb == cell_truth[f"HK_Performance!{ref}"]["resolved_rgb"]


def test_cells_by_rgb_groups_theme_cells(excel_fixture_path):
    """story #2 —— theme 解析后的 95B3D7 簇应在 cells_by_rgb 中聚到一起（3 格）。"""
    grid = _grids_by_sheet(excel_fixture_path)["HK_Performance"]
    groups = grid.cells_by_rgb()
    assert "95B3D7" in groups
    assert {c.cell_ref for c in groups["95B3D7"]} == {"B5", "C5", "D5"}


# ===========================================================================
# story #3 —— 合并单元格还原为多级表头语义
# ===========================================================================

def test_merge_origins_flagged_with_span(ground_truth, excel_fixture_path):
    """story #3 —— 三级合并表头锚点都标 is_merged_origin + 正确 merge_span。"""
    grid = _grids_by_sheet(excel_fixture_path)["MergedHeader"]
    for m in ground_truth["sheets"]["MergedHeader"]["merges_expect"]:
        cell = grid.get(m["origin"])
        assert cell.is_merged_origin is True
        assert cell.value == m["value"]
        assert cell.merge_span == tuple(m["span"])


def test_non_origin_cell_not_merged(excel_fixture_path):
    """story #3 —— 合并范围内的非锚点格不被错标为锚点（边界）。"""
    grid = _grids_by_sheet(excel_fixture_path)["MergedHeader"]
    data_cell = grid.get("A4")  # 数据区普通格
    assert data_cell.is_merged_origin is False
    assert data_cell.merge_span is None


def test_merged_header_data_row_values_intact(cell_truth, excel_fixture_path):
    """story #3 —— 合并表头下的数据行数字不张冠李戴（逐格值对齐真值）。"""
    grid = _grids_by_sheet(excel_fixture_path)["MergedHeader"]
    for ref in ("A4", "B4", "C4", "D4", "E4", "F4"):
        truth = cell_truth[f"MergedHeader!{ref}"]
        assert grid.get(ref).value == truth["value"]


# ===========================================================================
# story #5 —— 条件格式区域被检测：cf_affected + grid 告警，而非猜色
# ===========================================================================

def test_cf_cells_flagged_affected(ground_truth, excel_fixture_path):
    """story #5 —— CF 区域 B2:B5 的格都标 cf_affected=True。"""
    grid = _grids_by_sheet(excel_fixture_path)["CondFormat"]
    for ref in ("B2", "B3", "B4", "B5"):
        assert grid.get(ref).cf_affected is True


def test_cf_cells_have_no_resolved_color(excel_fixture_path):
    """story #5 —— CF 受影响格不得带静态颜色（resolved_rgb=None），宁可漏不可错。"""
    grid = _grids_by_sheet(excel_fixture_path)["CondFormat"]
    for ref in ("B2", "B3", "B4", "B5"):
        assert grid.get(ref).resolved_rgb is None


def test_cf_grid_emits_warning(excel_fixture_path):
    """story #5 —— 检测到条件格式必须在 grid.warnings 追加告警（不是静默）。"""
    grid = _grids_by_sheet(excel_fixture_path)["CondFormat"]
    assert len(grid.warnings) >= 1


def test_cf_cells_excluded_from_color_clustering(excel_fixture_path):
    """story #5 —— cf_affected 的格不参与 cells_by_rgb 聚类（来源不可靠）。"""
    grid = _grids_by_sheet(excel_fixture_path)["CondFormat"]
    clustered_refs = {
        c.cell_ref for cells in grid.cells_by_rgb().values() for c in cells
    }
    assert clustered_refs.isdisjoint({"B2", "B3", "B4", "B5"})


def test_cf_cell_values_still_extracted(cell_truth, excel_fixture_path):
    """story #5 —— 颜色跳过但值仍被完整抽出（只丢颜色语义，不丢数字）。"""
    grid = _grids_by_sheet(excel_fixture_path)["CondFormat"]
    for ref in ("B2", "B3", "B4", "B5"):
        assert grid.get(ref).value == cell_truth[f"CondFormat!{ref}"]["value"]


# ===========================================================================
# story #14 —— 转置表至少被完整读出网格（值不丢，不错读）
# ===========================================================================

def test_transposed_grid_values_complete(ground_truth, excel_fixture_path):
    """story #14 —— 转置表（指标在列、期间在行）逐格值全部读出且对齐真值。"""
    grid = _grids_by_sheet(excel_fixture_path)["Transposed"]
    transposed_cells = [
        c for c in ground_truth["cells"] if c["sheet"] == "Transposed"
    ]
    assert transposed_cells
    for t in transposed_cells:
        cell = grid.get(t["cell_ref"])
        assert cell is not None
        assert cell.value == t["value"]


def test_transposed_number_format_preserved(cell_truth, excel_fixture_path):
    """story #14 + #4 —— 转置表里的千分位数据格格式仍保留。"""
    grid = _grids_by_sheet(excel_fixture_path)["Transposed"]
    cell = grid.get("B2")
    assert cell.number_format == cell_truth["Transposed!B2"]["number_format"]


# ===========================================================================
# 跨表全量回归 —— 逐格真值全字段比对（值/颜色/格式/合并/CF）
# ===========================================================================

def test_all_ground_truth_cells_match(ground_truth, excel_fixture_path):
    """story #1–#5 —— 对 ground truth 全部 61 个逐格真值做外部行为全量比对。"""
    grids = _grids_by_sheet(excel_fixture_path)
    for t in ground_truth["cells"]:
        grid = grids[t["sheet"]]
        cell = grid.get(t["cell_ref"])
        assert cell is not None, f"{t['sheet']}!{t['cell_ref']} 缺失"
        assert cell.value == t["value"]
        assert cell.resolved_rgb == t["resolved_rgb"]
        assert cell.number_format == t["number_format"]
        assert cell.is_merged_origin == t["is_merged_origin"]
        expected_span = tuple(t["merge_span"]) if t["merge_span"] else None
        assert cell.merge_span == expected_span
        assert cell.cf_affected == t["cf_affected"]
