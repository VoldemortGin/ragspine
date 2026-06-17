"""PPT 增强抽取器测试（三期 PPT 线，TDD 红色阶段）。

只验证对外行为：给定合成 pptx fixture（styled_deck.pptx）与逐格 / note ground truth，
断言 ragspine.extraction.extractors.pptx_styled_extractor 两条抽取路径的输出，以及它与既有
color_semantics 资产（cluster_colors / MappingRegistry / apply_mapping）的集成 ——
即「与 Excel 同一套颜色映射机制」也能驱动 PPT 网格（story #13 核心）。
不断言 bbox、shape 索引等实现细节。

覆盖 PRD user stories：
    #12 PPT 叙述层（文本框 + 演讲者备注）里含数字句段抽取为 NoteFragment，
        glossary 命中指标代码、locator 精确回链。
    #13 PPT 原生表格的填充色解析为 StyledGrid（显式 RGB / theme 色 / 无填充），
        并复用 Excel 线的同色聚类 + 版本化映射注册表 -> product_line tags。
    #27 source_doc_id / source_file_hash 血缘写入每张 grid（版本 / 审计依据）。

红色预期：所有用例因 extract_grids / extract_note_fragments stub
raise NotImplementedError 而 FAIL（收集成功、无意外 PASS、无 collection error）。
"""

import os
import re

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import (
    ColorMapping,
    LegendEntry,
    MappingRegistry,
    apply_mapping,
    cluster_colors,
)
from ragspine.extraction.extractors.pptx_styled_extractor import (
    EXTRACTOR_VERSION,
    SOURCE_NOTES,
    SOURCE_TEXTBOX,
    NoteFragment,
    extract_grids,
    extract_note_fragments,
)
from ragspine.extraction.ir import StyledGrid


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _norm(text) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格（与抽取器约定一致）。"""
    return " ".join(str(text).split())


def _grids_by_sheet(path) -> dict[str, StyledGrid]:
    """把抽取出的 grid 列表按 sheet 名索引。"""
    return {g.sheet: g for g in extract_grids(path)}


def _slide_truth(pptx_ground_truth: dict, slide_key: str) -> dict:
    """取某幻灯片（'slide1' / 'slide2'）的逐格真值块。"""
    return pptx_ground_truth["slides"][slide_key]


# ===========================================================================
# story #13 —— 每张原生表一个 StyledGrid，sheet 命名 'slide{N}_table{M}'
# ===========================================================================

def test_extract_returns_list_of_styled_grids(styled_deck_path):
    """story #13 —— styled_deck.pptx 抽取结果是 list[StyledGrid]，含两张表。"""
    grids = extract_grids(styled_deck_path)
    assert isinstance(grids, list)
    assert all(isinstance(g, StyledGrid) for g in grids)
    assert len(grids) == 2


def test_sheet_names_follow_slide_table_pattern(styled_deck_path):
    """story #13 —— 所有 sheet 名遵循 'slide{N}_table{M}'（N、M 均 1-based 正整数）。"""
    pattern = re.compile(r"^slide([1-9]\d*)_table([1-9]\d*)$")
    for g in extract_grids(styled_deck_path):
        assert pattern.match(g.sheet) is not None, f"非法 sheet 命名: {g.sheet!r}"


def test_expected_sheets_present(styled_deck_path, pptx_ground_truth):
    """story #13 —— slide1_table1 / slide2_table1 两张表都被抽出。"""
    sheets = {g.sheet for g in extract_grids(styled_deck_path)}
    assert _slide_truth(pptx_ground_truth, "slide1")["sheet"] in sheets
    assert _slide_truth(pptx_ground_truth, "slide2")["sheet"] in sheets


def test_accepts_str_and_path_equivalently(styled_deck_path):
    """story #13 —— str 与 Path 入参产出一致的 sheet 集合（接口宽容）。"""
    by_path = {g.sheet for g in extract_grids(styled_deck_path)}
    by_str = {g.sheet for g in extract_grids(str(styled_deck_path))}
    assert by_path == by_str


# ===========================================================================
# story #13 —— 表维度与逐格值全量比对（含转置表如实产网格）
# ===========================================================================

def test_slide1_table_dimensions_match_truth(styled_deck_path, pptx_ground_truth):
    """story #13 —— slide1_table1 的逻辑行列数与真值一致（4 行 × 4 列）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    assert grid.n_rows == truth["n_rows"]
    assert grid.n_cols == truth["n_cols"]


def test_transposed_table_dimensions_match_truth(styled_deck_path, pptx_ground_truth):
    """story #13 —— 转置表（期间在行、指标在列）如实产网格：维度真值 3 行 × 4 列。

    刁钻形态：抽取器不做语义判断（语义归下游），只须 n_rows/n_cols 如实反映真实维度。
    """
    truth = _slide_truth(pptx_ground_truth, "slide2")
    assert truth.get("orientation") == "transposed"
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    assert grid.n_rows == truth["n_rows"]
    assert grid.n_cols == truth["n_cols"]


def test_cell_refs_use_R_C_notation(styled_deck_path, pptx_ground_truth):
    """story #13 —— 单元格用 'R{行}C{列}' 坐标（1-based），区别于 Excel 'C4' 风格。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref in ("R1C2", "R2C1", "R4C4"):
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.cell_ref == ref


def test_slide1_all_truth_cells_values_match(styled_deck_path, pptx_ground_truth):
    """story #13 —— slide1 逐格值全量比对（每格存在、值空白归一化后一致）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref, expected in truth["cells"].items():
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.cell_ref == ref
        assert _norm(cell.value) == _norm(expected["value"]), (
            f"{ref}: got {cell.value!r} expect {expected['value']!r}"
        )


def test_slide2_transposed_all_truth_cells_values_match(styled_deck_path, pptx_ground_truth):
    """story #13 —— 转置表逐格值全量比对（指标列 REVENUE/NEWSALES/PROFIT、期间行如实产出）。"""
    truth = _slide_truth(pptx_ground_truth, "slide2")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref, expected in truth["cells"].items():
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert _norm(cell.value) == _norm(expected["value"]), (
            f"{ref}: got {cell.value!r} expect {expected['value']!r}"
        )


def test_cell_text_is_whitespace_normalized(styled_deck_path):
    """story #13 —— 文本格不带首尾空白、内部无连续多空白（已归一化）。"""
    for g in extract_grids(styled_deck_path):
        for cell in g.iter_cells():
            text = cell.value
            if not isinstance(text, str):
                continue
            assert text == text.strip(), f"首尾空白未归一化: {text!r}"
            assert "  " not in text, f"内部连续空白未折叠: {text!r}"


# ===========================================================================
# story #13 —— 填充色解析：显式 RGB / theme 色 / 无填充 None 全量比对
# ===========================================================================

def test_slide1_all_resolved_rgb_match_truth(styled_deck_path, pptx_ground_truth):
    """story #13 —— slide1 每格 resolved_rgb 与真值逐格一致（显式 RGB / theme / None 都核）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref, expected in truth["cells"].items():
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.resolved_rgb == expected["resolved_rgb"], (
            f"{ref}: got {cell.resolved_rgb!r} expect {expected['resolved_rgb']!r}"
        )


def test_explicit_rgb_fill_yellow_and_green(styled_deck_path, pptx_ground_truth):
    """story #13 —— 显式 RGB 填充直取：黄行 FFFF00、绿行 92D050（与 Excel 同色编码）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref in ("R2C1", "R2C2", "R2C3", "R2C4"):
        assert grid.get(ref).resolved_rgb == "FFFF00", ref
    for ref in ("R3C1", "R3C2", "R3C3", "R3C4"):
        assert grid.get(ref).resolved_rgb == "92D050", ref


def test_theme_color_cell_resolved_via_theme_xml(styled_deck_path, pptx_ground_truth):
    """story #13 —— theme 色格（accent1）须经 ppt/theme1.xml 解析为真实 RGB（不丢色）。

    真值 R4C2 = theme accent1 -> 解析后的 resolved_rgb（如 4F81BD），与 ground truth
    theme.accent1_resolved_rgb 一致。
    """
    truth = _slide_truth(pptx_ground_truth, "slide1")
    accent1 = pptx_ground_truth["theme"]["accent1_resolved_rgb"]
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    cell = grid.get("R4C2")
    assert cell is not None
    assert cell.resolved_rgb == accent1
    assert cell.resolved_rgb == truth["cells"]["R4C2"]["resolved_rgb"]


def test_no_fill_cells_resolve_to_none(styled_deck_path, pptx_ground_truth):
    """story #13 —— 无填充格 resolved_rgb 为 None（不臆造颜色）：R4C3 / R4C4 等。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for ref in ("R4C3", "R4C4", "R1C2"):
        assert truth["cells"][ref]["resolved_rgb"] is None  # 真值前提
        assert grid.get(ref).resolved_rgb is None, ref


def test_transposed_table_all_cells_no_fill(styled_deck_path, pptx_ground_truth):
    """story #13 —— 转置表整表无填充：每格 resolved_rgb 恒为 None。"""
    truth = _slide_truth(pptx_ground_truth, "slide2")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    for cell in grid.iter_cells():
        assert cell.resolved_rgb is None, cell.cell_ref


def test_native_table_cells_have_no_ocr_confidence(styled_deck_path):
    """story #13 —— 原生表是确定性抽取，无 OCR 置信概念：每格 confidence 恒为 None。"""
    for g in extract_grids(styled_deck_path):
        for cell in g.iter_cells():
            assert cell.confidence is None, cell.cell_ref


# ===========================================================================
# story #27 —— 血缘：source_doc_id / source_file_hash 写入每张 grid
# ===========================================================================

def test_grids_carry_source_doc_id(styled_deck_path):
    """story #27 —— 每张 grid 的 source_doc_id 为源文件名（下游血缘根）。"""
    grids = extract_grids(styled_deck_path)
    assert grids
    for g in grids:
        assert g.source_doc_id == styled_deck_path.name


def test_grids_carry_nonempty_source_hash(styled_deck_path):
    """story #27 —— 每张 grid 的 source_file_hash 为非空十六进制串。"""
    grids = extract_grids(styled_deck_path)
    assert grids
    for g in grids:
        assert isinstance(g.source_file_hash, str)
        assert len(g.source_file_hash) > 0
        int(g.source_file_hash, 16)  # 必须是合法十六进制


def test_grids_share_same_source_hash(styled_deck_path):
    """story #27 —— 同一文件抽出的所有 grid 共享同一个 source_file_hash（确定性血缘）。"""
    hashes = {g.source_file_hash for g in extract_grids(styled_deck_path)}
    assert len(hashes) == 1


# ===========================================================================
# story #12 —— extract_note_fragments：含数字句段、source_kind、glossary、locator
# ===========================================================================

def test_extract_note_fragments_returns_list(styled_deck_path):
    """story #12 —— 返回 list[NoteFragment]（叙述层数字线索的统一载体）。"""
    frags = extract_note_fragments(styled_deck_path)
    assert isinstance(frags, list)
    assert all(isinstance(f, NoteFragment) for f in frags)


def test_note_fragments_count_matches_truth(styled_deck_path, pptx_ground_truth):
    """story #12 —— 只收含数字句段：恰好 2 条（REVENUE 文本框 + PROFIT 备注）。

    刁钻反例：slide2 的无数字文本框 'Performance overview...' 不得被收。
    """
    frags = extract_note_fragments(styled_deck_path)
    assert len(frags) == len(pptx_ground_truth["note_fragments"]) == 2


def test_note_fragments_texts_match_truth(styled_deck_path, pptx_ground_truth):
    """story #12 —— 句段文本（空白归一化）与真值逐条吻合。"""
    got = {_norm(f.text) for f in extract_note_fragments(styled_deck_path)}
    expected = {_norm(nf["text"]) for nf in pptx_ground_truth["note_fragments"]}
    assert got == expected


def test_note_fragments_all_contain_digit(styled_deck_path):
    """story #12 —— 每条抽出的句段都含数字（确定性规则：含 digit 才收）。"""
    for f in extract_note_fragments(styled_deck_path):
        assert any(ch.isdigit() for ch in f.text), f.text


def test_textbox_fragment_source_kind_and_slide(styled_deck_path):
    """story #12 —— slide1 含数字文本框来源 source_kind='textbox'、slide_no=1。"""
    frags = extract_note_fragments(styled_deck_path)
    by_kind = {f.source_kind: f for f in frags}
    assert SOURCE_TEXTBOX in by_kind
    tb = by_kind[SOURCE_TEXTBOX]
    assert tb.slide_no == 1
    assert "REVENUE" in tb.text


def test_notes_fragment_source_kind_and_slide(styled_deck_path):
    """story #12 —— slide2 演讲者备注来源 source_kind='notes'、slide_no=2。"""
    frags = extract_note_fragments(styled_deck_path)
    by_kind = {f.source_kind: f for f in frags}
    assert SOURCE_NOTES in by_kind
    nt = by_kind[SOURCE_NOTES]
    assert nt.slide_no == 2
    assert "PROFIT" in nt.text


def test_note_fragments_source_kind_values_valid(styled_deck_path):
    """story #12 —— source_kind 取值只能是约定常量 textbox / notes。"""
    for f in extract_note_fragments(styled_deck_path):
        assert f.source_kind in (SOURCE_TEXTBOX, SOURCE_NOTES)


def test_glossary_hits_revenue_and_profit(styled_deck_path, pptx_ground_truth):
    """story #12 —— glossary 确定性命中：REVENUE 文本框 -> ['REVENUE']、PROFIT 备注 -> ['PROFIT']。"""
    frags = extract_note_fragments(styled_deck_path)
    got = {f.slide_no: f.glossary_hits for f in frags}
    expected = {nf["slide_no"]: nf["glossary_hits"] for nf in pptx_ground_truth["note_fragments"]}
    assert got == expected


def test_glossary_hits_union_is_revenue_profit(styled_deck_path):
    """story #12 —— 全体命中集合恰为 {REVENUE, PROFIT}（无漏报、无臆造其它指标）。"""
    hits = set()
    for f in extract_note_fragments(styled_deck_path):
        hits.update(f.glossary_hits)
    assert hits == {"REVENUE", "PROFIT"}


def test_note_locator_points_to_correct_slide(styled_deck_path):
    """story #12 —— locator 精确回链对应 slide（textbox -> slide1*、notes -> slide2*）。

    只断言 locator 含正确幻灯片号与来源种类线索，不绑定具体格式串。
    """
    frags = extract_note_fragments(styled_deck_path)
    for f in frags:
        assert isinstance(f.locator, str) and f.locator
        assert str(f.slide_no) in f.locator
        if f.source_kind == SOURCE_NOTES:
            assert "notes" in f.locator.lower()
        else:
            assert "textbox" in f.locator.lower()


def test_note_fragments_ordered_by_slide(styled_deck_path):
    """story #12 —— 返回顺序按幻灯片号升序（textbox@slide1 在 notes@slide2 之前）。"""
    frags = extract_note_fragments(styled_deck_path)
    slide_nos = [f.slide_no for f in frags]
    assert slide_nos == sorted(slide_nos)


def test_extractor_version_tag(styled_deck_path):
    """story #27 —— 抽取器版本标识为契约约定值，且抽取在该版本下可正常产出网格。"""
    assert EXTRACTOR_VERSION == "pptx_styled_v0"
    grids = extract_grids(styled_deck_path)  # 红色阶段：raise NotImplementedError
    assert grids


# ===========================================================================
# story #13 —— 与 color_semantics 集成：同色聚类 + 版本化映射 -> product_line tags
# （PPT 网格走与 Excel 完全相同的颜色映射机制）
# ===========================================================================

def _product_line_mapping(scope: str, *, status: str) -> ColorMapping:
    """组装与 Excel 线同义的 product_line 映射（黄=新、绿=成熟）。"""
    return ColorMapping(
        scope=scope,
        entries=[
            LegendEntry(rgb="FFFF00", meaning="黄色=新产品线",
                        tag_key="product_line", tag_value="new", source_ref="legend_new"),
            LegendEntry(rgb="92D050", meaning="绿色=成熟产品线",
                        tag_key="product_line", tag_value="mature", source_ref="legend_mature"),
        ],
        status=status,
    )


def test_cluster_colors_on_pptx_grid_groups_match(styled_deck_path, pptx_ground_truth):
    """story #13 —— 对 PPT 抽出的网格做同色聚类，分组（rgb→cell_refs）与真值着色格吻合。

    黄簇 = slide1 第 2 行 4 格、绿簇 = 第 3 行 4 格、theme(accent1)簇 = R4C2 单格。
    """
    truth = _slide_truth(pptx_ground_truth, "slide1")
    accent1 = pptx_ground_truth["theme"]["accent1_resolved_rgb"]
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    got = {c.rgb: sorted(c.cell_refs) for c in cluster_colors(grid)}
    expected = {
        "FFFF00": ["R2C1", "R2C2", "R2C3", "R2C4"],
        "92D050": ["R3C1", "R3C2", "R3C3", "R3C4"],
        accent1: ["R4C2"],
    }
    assert got == expected


def test_cluster_colors_sorted_size_desc_on_pptx(styled_deck_path, pptx_ground_truth):
    """story #13 —— 聚类报告按簇大小降序（黄/绿各 4 格在前，单格 theme 簇在后）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    clusters = cluster_colors(grid)
    counts = [c.count for c in clusters]
    assert counts == sorted(counts, reverse=True)
    assert clusters[0].count == 4


def test_transposed_pptx_grid_clusters_empty(styled_deck_path, pptx_ground_truth):
    """story #13 —— 无任何填充色的转置表，聚类返回空列表（不臆造）。"""
    truth = _slide_truth(pptx_ground_truth, "slide2")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    assert cluster_colors(grid) == []


def test_apply_unconfirmed_mapping_on_pptx_empty_and_warns(styled_deck_path, pptx_ground_truth):
    """story #13 —— PPT 网格上应用未确认（draft）映射：返回空 tags 并追加 grid 告警（不静默入库）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    mapping = _product_line_mapping(scope=truth["sheet"], status="draft")
    warnings_before = len(grid.warnings)
    result = apply_mapping(grid, mapping)
    assert result == {}
    assert len(grid.warnings) > warnings_before


def test_registry_confirm_then_apply_yellow_is_new(styled_deck_path, pptx_ground_truth, tmp_db_path):
    """story #13 —— 注册表 confirm 后取 active 映射应用到 PPT 网格：黄行 -> product_line=new。

    走与 Excel 完全相同的机制：register_draft -> confirm -> get_active -> apply_mapping。
    """
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]

    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        scope = truth["sheet"]
        version = reg.register_draft(_product_line_mapping(scope=scope, status="draft"))
        reg.confirm(scope, version, actor="sme_fin", note="PPT 同 Excel 映射")
        active = reg.get_active(scope)
        assert active is not None and active.status == "active"
        result = apply_mapping(grid, active)
    finally:
        reg.close()

    for ref in ("R2C1", "R2C2", "R2C3", "R2C4"):
        assert result.get(ref) == {"product_line": "new"}, ref


def test_registry_confirm_then_apply_green_is_mature(styled_deck_path, pptx_ground_truth, tmp_db_path):
    """story #13 —— confirm 后应用：绿行 -> product_line=mature（与黄行同机制反向验证）。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]

    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        scope = truth["sheet"]
        version = reg.register_draft(_product_line_mapping(scope=scope, status="draft"))
        reg.confirm(scope, version, actor="sme_fin")
        result = apply_mapping(grid, reg.get_active(scope))
    finally:
        reg.close()

    for ref in ("R3C1", "R3C2", "R3C3", "R3C4"):
        assert result.get(ref) == {"product_line": "mature"}, ref


def test_apply_active_unmapped_color_and_nofill_get_no_tag(styled_deck_path, pptx_ground_truth):
    """story #13 —— 边界：映射未覆盖的 theme 色（R4C2）与无填充格（R4C3/R4C4/R1C2）不打 tag。"""
    truth = _slide_truth(pptx_ground_truth, "slide1")
    grid = _grids_by_sheet(styled_deck_path)[truth["sheet"]]
    mapping = _product_line_mapping(scope=truth["sheet"], status="active")
    result = apply_mapping(grid, mapping)
    for ref in ("R4C2", "R4C3", "R4C4", "R1C2"):
        assert not result.get(ref), ref
