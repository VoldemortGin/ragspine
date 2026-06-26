"""PptspineGridExtractor（W3c）测试：.pptx 表格 -> StyledGrid + registry 备选分发。

只验证对外行为：给定就地（纯 zipfile，make_pptx）合成的最小 .pptx，断言 extract_grids
产出的 StyledGrid（sheet/cell_ref/value/血缘/合并跨度）与 registry 的【备选选择项】分发。

W3c 是 additive opt-in：pptspine 提供更富的表合并（gridSpan/rowSpan/hMerge/vMerge），
但**默认 .pptx 仍走 python-pptx**（pptx_styled，保 color/chart/note 不丢）。本文件因此
也冻结「默认不变」这条铁律：registry 的 .pptx / PPTX_MIME 仍分发到 pptx_styled，pptspine
只在显式备选 key 'pptx+pptspine' 下分发。

pptspine 是可选 [ppt] extra；未装则全文件 skip（真实解析测试需要它，离线纯 Rust）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

# pptspine 是可选 [ppt] extra；未装则全文件 skip（真实解析测试需要它，离线纯 Rust）。
pptspine = pytest.importorskip("pptspine", reason="pptspine 未安装（pip install rag-spine[ppt]）")


def test_extract_grids_maps_table_to_styled_grid(make_pptx, tmp_path):
    """user story：作为有 PPT 报告的用户，.pptx 的表格应被 pptspine 解析为带单元格
    定位的 StyledGrid，让 PPT 数字进结构化通路（与 python-pptx 路同一 IR）。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "deck.pptx"
    make_pptx(p, [
        [("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])],
    ])
    grids = extract_grids(p)
    assert len(grids) == 1
    g = grids[0]
    assert g.sheet == "slide1_table1"
    assert g.source_doc_id == "deck.pptx"
    assert g.source_file_hash  # 非空血缘
    assert g.n_rows == 2 and g.n_cols == 2
    assert g.get("R1C1").value == "ACME Hong Kong"
    assert g.get("R1C2").value == "FY2024"
    assert g.get("R2C1").value == "REVENUE"
    assert g.get("R2C2").value == "2680"
    # 颜色/富填充语义留 W3d，resolved_rgb 恒 None（与 docspine / pdf_spine 同口径）。
    assert all(c.resolved_rgb is None for c in g.iter_cells())


def test_every_cell_carries_provenance(make_pptx, tmp_path):
    """user story：每个 StyledCell 带非空 cell_ref locator + grid 携 source_doc_id，
    provenance 一致性包不被 pptx/pptspine 通路破坏。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "lineage.pptx"
    make_pptx(p, [[("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])]])
    g = extract_grids(p)[0]
    assert g.source_doc_id == "lineage.pptx"
    for c in g.iter_cells():
        assert c.cell_ref.startswith("R")


def test_no_tables_returns_empty(make_pptx, tmp_path):
    """user story：纯文本无表格的 .pptx -> 结构化通路返回 []（叙述数字留给叙事通路）。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "prose.pptx"
    make_pptx(p, [[("text", "All narrative, no tables here.")]])
    assert extract_grids(p) == []


def test_multiple_slides_get_sequential_sheets(make_pptx, tmp_path):
    """user story：多页表格得到 slide{N}_table{M} 稳定 sheet 名（与 pptx_styled 对齐），
    sheet 的 slide 号跟真实幻灯片号（无表页留空号、不错位），便于 citation 回指到「页+表」。

    注：pptspine 0.1.0 每页只稳定返回首张表（graphicFrame 解析限制；见 extractor
    docstring 的诚实记录）—— 这正是 pptspine 留作 opt-in、默认仍 python-pptx 的原因之一。
    本用例因此跨多页各一张表来冻结 slide 号 + 表号的命名约定。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "multi.pptx"
    make_pptx(p, [
        [("table", [["A", "B"]])],            # slide1 -> slide1_table1
        [("text", "no table on this slide")],  # slide2 -> 无表，留空号
        [("text", "intro"), ("table", [["E", "F"]])],  # slide3 -> slide3_table1
    ])
    grids = extract_grids(p)
    assert [g.sheet for g in grids] == ["slide1_table1", "slide3_table1"]


def test_grid_span_recorded_as_merge_span(make_pptx, tmp_path):
    """W3c「合并跨度尽量保留」：gridSpan=2 的表头格 -> is_merged_origin + merge_span=(1,2)；
    被吞的水平延续格（hMerge）空文本、不入稀疏网格。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "merged.pptx"
    make_pptx(p, [[("table", [
        [{"text": "ACME Hong Kong", "gridspan": 2}, {"hmerge": True}],
        ["FY2023", "FY2024"],
    ])]])
    g = extract_grids(p)[0]
    origin = g.get("R1C1")
    assert origin.value == "ACME Hong Kong"
    assert origin.is_merged_origin is True
    assert origin.merge_span == (1, 2)
    assert g.get("R1C2") is None  # 水平延续格被吞，不入网格


def test_rowspan_recorded_as_vertical_span(make_pptx, tmp_path):
    """rowSpan=2 的锚点 -> 纵向合并跨度 merge_span=(2,1)；被吞的纵向延续格（vMerge）
    空文本、不入网格。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "vmerged.pptx"
    make_pptx(p, [[("table", [
        [{"text": "Region", "rowspan": 2}, "FY2024"],
        [{"vmerge": True}, "2680"],
    ])]])
    g = extract_grids(p)[0]
    origin = g.get("R1C1")
    assert origin.value == "Region"
    assert origin.is_merged_origin is True
    assert origin.merge_span == (2, 1)
    assert g.get("R2C1") is None  # 纵向延续格被吞，不入网格
    assert g.get("R2C2").value == "2680"


def test_extractor_class_version_and_protocol(make_pptx, tmp_path):
    """user story：PptspineGridExtractor 带 version='pptspine@1'（写入 fact 血缘），
    且结构性满足 registry 的 Extractor 协议（可被注册分发）。"""
    from ragspine.extraction.extractors.pptspine_extractor import PptspineGridExtractor
    from ragspine.extraction.registry import Extractor

    ext = PptspineGridExtractor()
    assert ext.version == "pptspine@1"
    assert isinstance(ext, Extractor)  # runtime_checkable 结构匹配
    p = tmp_path / "c.pptx"
    make_pptx(p, [[("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])]])
    grids = ext.extract(p)
    assert grids[0].get("R2C2").value == "2680"


def test_registry_alternative_selector_dispatches_pptspine(make_pptx, tmp_path):
    """W3c 接入方式之一：registry 备选选择项 'pptx+pptspine' 分发到 pptspine 抽取器，
    无需改任何路由代码（内置 loader 缝）。"""
    from ragspine.extraction.registry import (
        PPTX_PPTSPINE_SELECTOR,
        get_extractor,
        registered_mimes,
    )

    assert PPTX_PPTSPINE_SELECTOR in registered_mimes()
    p = tmp_path / "r.pptx"
    make_pptx(p, [[("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])]])
    ext = get_extractor(PPTX_PPTSPINE_SELECTOR)
    grids = ext.extract(p)
    assert grids[0].get("R1C1").value == "ACME Hong Kong"


def test_registry_default_pptx_stays_python_pptx(make_pptx, tmp_path):
    """铁律（additive 不回归）：默认 .pptx / PPTX_MIME 仍分发到 python-pptx 的
    pptx_styled —— pptspine 只是 opt-in 备选，**绝不**替换默认（保 color/chart/note）。"""
    from ragspine.extraction.registry import PPTX_MIME, get_extractor

    # 内置 _FunctionExtractor 暴露被包装的内置抽取器名，用于断言默认仍是 pptx_styled。
    for key in (".pptx", PPTX_MIME):
        ext = get_extractor(key)
        assert getattr(ext, "name", "") == "pptx_styled"


# ===========================================================================
# W3d（富表 IR）：pptspine 单元格底色 -> resolved_rgb（PPTX 表格无嵌套，故只富填充）。
# ===========================================================================


def test_cell_fill_resolved_into_rgb(make_pptx, tmp_path):
    """W3d user story 2：PPT 表格单元格 solidFill/srgbClr 经 pptspine 的 cell['fill'] 解析
    进 StyledCell.resolved_rgb（'RRGGBB' 大写十六进制），无底色为 None。"""
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "filled.pptx"
    make_pptx(p, [[("table", [
        [{"text": "NEW", "fill": "FFFF00"}, {"text": "MATURE", "fill": "92d050"}],
        ["plain", "2680"],
    ])]])
    g = extract_grids(p)[0]
    assert g.get("R1C1").resolved_rgb == "FFFF00"
    assert g.get("R1C2").resolved_rgb == "92D050"  # 小写源色归一为大写
    assert g.get("R2C1").resolved_rgb is None
    assert g.get("R2C2").resolved_rgb is None


def test_cell_fill_flows_into_color_semantics_path(make_pptx, tmp_path):
    """W3d：着色单元格经既有 cells_by_rgb()/cluster_colors() 同色聚类通路被消费 ——
    证明 pptspine 颜色真正流进 color-semantics（SME-gated）通路。"""
    from ragspine.extraction.color.color_semantics import cluster_colors
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    p = tmp_path / "legend.pptx"
    make_pptx(p, [[("table", [
        [{"text": "A", "fill": "FFFF00"}, {"text": "B", "fill": "FFFF00"}],
        [{"text": "C", "fill": "92D050"}, "plain"],
    ])]])
    g = extract_grids(p)[0]
    by_rgb = g.cells_by_rgb()
    assert set(by_rgb) == {"FFFF00", "92D050"}
    assert {c.cell_ref for c in by_rgb["FFFF00"]} == {"R1C1", "R1C2"}
    clusters = cluster_colors(g)
    assert clusters[0].rgb == "FFFF00" and clusters[0].count == 2
