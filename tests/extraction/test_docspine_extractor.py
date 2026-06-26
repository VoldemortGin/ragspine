"""DocspineGridExtractor（W3b）红色测试：.docx 表格 -> StyledGrid + 注册表分发。

只验证对外行为：给定就地（纯 zipfile）合成的最小 .docx，断言 extract_grids 产出的
StyledGrid（sheet/cell_ref/value/血缘/合并跨度）与 registry 的 .docx 分发。

红色预期：`docspine_extractor` 模块尚不存在 / registry 未登记 .docx -> import/断言 FAIL。
import 放进测试体内，使其作为用例 FAILURE 暴露（沿用仓库红色阶段约定）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

# docspine 是可选 [doc] extra；未装则全文件 skip（真实解析测试需要它，离线纯 Rust）。
docspine = pytest.importorskip("docspine", reason="docspine 未安装（pip install rag-spine[doc]）")


def test_extract_grids_maps_table_to_styled_grid(make_docx, tmp_path):
    """user story：作为有 Word 报告的用户，.docx 的表格应被解析为带单元格定位的
    StyledGrid，让 PPT/Excel 之外的 Word 数字也能进结构化通路。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "report.docx"
    make_docx(p, [
        ("para", "FY2024 Hong Kong performance review"),
        ("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]]),
        ("para", "Closing remarks."),
    ])
    grids = extract_grids(p)
    assert len(grids) == 1
    g = grids[0]
    assert g.sheet == "table1"
    assert g.source_doc_id == "report.docx"
    assert g.source_file_hash  # 非空血缘
    assert g.n_rows == 2 and g.n_cols == 2
    assert g.get("R1C1").value == "ACME Hong Kong"
    assert g.get("R1C2").value == "FY2024"
    assert g.get("R2C1").value == "REVENUE"
    assert g.get("R2C2").value == "2680"
    # 颜色语义不属 docx（W3b），resolved_rgb 恒 None
    assert all(c.resolved_rgb is None for c in g.iter_cells())


def test_every_cell_carries_provenance(make_docx, tmp_path):
    """user story：每个 StyledCell 都带非空 cell_ref locator + grid 携 source_doc_id，
    provenance 一致性包不被 docx 通路破坏。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "lineage.docx"
    make_docx(p, [("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])])
    g = extract_grids(p)[0]
    assert g.source_doc_id == "lineage.docx"
    for c in g.iter_cells():
        assert c.cell_ref.startswith("R")


def test_no_tables_returns_empty(make_docx, tmp_path):
    """user story：纯正文无表格的 .docx -> 结构化通路返回 []（数字留给叙事通路）。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "prose.docx"
    make_docx(p, [("para", "All narrative, no tables here.")])
    assert extract_grids(p) == []


def test_multiple_tables_get_sequential_sheets(make_docx, tmp_path):
    """user story：多张表按文档顺序得到 table1 / table2 ... 稳定 sheet 名，便于回指。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "multi.docx"
    make_docx(p, [
        ("table", [["A", "B"]]),
        ("para", "between"),
        ("table", [["C", "D"]]),
    ])
    grids = extract_grids(p)
    assert [g.sheet for g in grids] == ["table1", "table2"]


def test_grid_span_recorded_as_merge_span(make_docx, tmp_path):
    """W3b「合并跨度尽量保留」：gridSpan=2 的表头格 -> is_merged_origin + merge_span=(1,2)。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "merged.docx"
    make_docx(p, [
        ("table", [
            [{"text": "ACME Hong Kong", "gridspan": 2}],
            ["FY2023", "FY2024"],
        ]),
    ])
    g = extract_grids(p)[0]
    origin = g.get("R1C1")
    assert origin.value == "ACME Hong Kong"
    assert origin.is_merged_origin is True
    assert origin.merge_span == (1, 2)


def test_vmerge_recorded_as_vertical_span(make_docx, tmp_path):
    """vMerge restart/continue -> 纵向合并跨度 merge_span=(2,1)（续格空文本被吞、不入网格）。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "vmerged.docx"
    make_docx(p, [
        ("table", [
            [{"text": "Region", "vmerge": "restart"}, "FY2024"],
            [{"text": "", "vmerge": "continue"}, "2680"],
        ]),
    ])
    g = extract_grids(p)[0]
    origin = g.get("R1C1")
    assert origin.value == "Region"
    assert origin.is_merged_origin is True
    assert origin.merge_span == (2, 1)
    assert g.get("R2C2").value == "2680"


def test_extractor_class_version_and_protocol(make_docx, tmp_path):
    """user story：DocspineGridExtractor 带 version='docspine@1'（写入 fact 血缘），
    且结构性满足 registry 的 Extractor 协议（可被注册分发）。"""
    from ragspine.extraction.extractors.docspine_extractor import DocspineGridExtractor
    from ragspine.extraction.registry import Extractor

    ext = DocspineGridExtractor()
    assert ext.version == "docspine@1"
    assert isinstance(ext, Extractor)  # runtime_checkable 结构匹配
    p = tmp_path / "c.docx"
    make_docx(p, [("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])])
    grids = ext.extract(p)
    assert grids[0].get("R2C2").value == "2680"


def test_registry_dispatches_docx(make_docx, tmp_path):
    """user story：.docx + 其 mime 经 registry get_extractor 分发到 docspine 抽取器，
    无需改任何路由代码（register_extractor / 内置 loader 缝）。"""
    from ragspine.extraction.registry import DOCX_MIME, get_extractor, registered_mimes

    assert ".docx" in registered_mimes()
    assert DOCX_MIME in registered_mimes()
    p = tmp_path / "r.docx"
    make_docx(p, [("table", [["ACME Hong Kong", "FY2024"], ["REVENUE", "2680"]])])
    for key in (".docx", DOCX_MIME):
        ext = get_extractor(key)
        grids = ext.extract(p)
        assert grids[0].get("R1C1").value == "ACME Hong Kong"


# ===========================================================================
# W3d（富表 IR）：单元格底纹色 -> resolved_rgb；嵌套表 -> 独立 StyledGrid。
# ===========================================================================


def test_cell_fill_resolved_into_rgb(make_docx, tmp_path):
    """W3d user story 2：Word 表格单元格底纹色（<w:shd w:fill>）经 docspine 的 cell['fill']
    解析进 StyledCell.resolved_rgb（'RRGGBB' 大写十六进制），无底纹为 None。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "filled.docx"
    make_docx(p, [
        ("table", [
            [{"text": "NEW", "fill": "FFFF00"}, {"text": "MATURE", "fill": "92d050"}],
            ["plain", "2680"],
        ]),
    ])
    g = extract_grids(p)[0]
    assert g.get("R1C1").resolved_rgb == "FFFF00"
    # 小写源色归一为大写（IR 契约：resolved_rgb 恒大写 RRGGBB）。
    assert g.get("R1C2").resolved_rgb == "92D050"
    # 无底纹格仍 None（不臆造颜色）。
    assert g.get("R2C1").resolved_rgb is None
    assert g.get("R2C2").resolved_rgb is None


def test_cell_fill_flows_into_color_semantics_path(make_docx, tmp_path):
    """W3d：着色单元格经既有 cells_by_rgb()/cluster_colors() 同色聚类通路被消费 ——
    证明 docx 颜色真正流进 color-semantics（SME-gated）通路，而非止步抽取层。"""
    from ragspine.extraction.color.color_semantics import cluster_colors
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "legend.docx"
    make_docx(p, [
        ("table", [
            [{"text": "A", "fill": "FFFF00"}, {"text": "B", "fill": "FFFF00"}],
            [{"text": "C", "fill": "92D050"}, "plain"],
        ]),
    ])
    g = extract_grids(p)[0]
    by_rgb = g.cells_by_rgb()
    assert set(by_rgb) == {"FFFF00", "92D050"}
    assert {c.cell_ref for c in by_rgb["FFFF00"]} == {"R1C1", "R1C2"}
    clusters = cluster_colors(g)
    # 最大簇（黄，2 格）排在前，证明聚类报告确实收到 docx 的 resolved_rgb。
    assert clusters[0].rgb == "FFFF00" and clusters[0].count == 2


def test_nested_table_emitted_as_independent_grid(make_docx, tmp_path):
    """W3d：单元格内嵌套表不再只发 warning，而是作为独立 StyledGrid 产出，sheet 命名
    体现父子（table{M}.cell{r}_{c}.nested{k}），locator 链可追溯，绝不静默丢。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "nested.docx"
    make_docx(p, [
        ("table", [
            ["Region", "FY2024"],
            [{"text": "OUTER", "nested": [
                [["INNER-A", "INNER-B"], ["REVENUE", "2680"]],
            ]}, "tail"],
        ]),
    ])
    grids = extract_grids(p)
    # 父表 + 一张嵌套表 = 2 张 StyledGrid。
    sheets = [g.sheet for g in grids]
    assert sheets == ["table1", "table1.cell2_1.nested1"]
    parent, nested = grids
    # 父格仍承载自身段落文本（嵌套富结构不污染父格值）。
    assert parent.get("R2C1").value == "OUTER"
    # 嵌套表作为独立网格被完整表示，血缘与父同源。
    assert nested.source_doc_id == "nested.docx"
    assert nested.source_file_hash == parent.source_file_hash
    assert nested.get("R1C1").value == "INNER-A"
    assert nested.get("R2C2").value == "2680"
    # 父网格留 breadcrumb 告警指向独立子网格（provenance 可追溯，never silently dropped）。
    assert any("table1.cell2_1.nested1" in w for w in parent.warnings)


def test_deeply_nested_table_recurses(make_docx, tmp_path):
    """W3d：嵌套表自身再含嵌套表时递归产出，sheet 名链式体现祖父->父->子层级。"""
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    p = tmp_path / "deep.docx"
    make_docx(p, [
        ("table", [
            [{"text": "L0", "nested": [
                [[{"text": "L1", "nested": [
                    [["L2", "leaf"]],
                ]}]],
            ]}],
        ]),
    ])
    sheets = [g.sheet for g in extract_grids(p)]
    assert sheets == [
        "table1",
        "table1.cell1_1.nested1",
        "table1.cell1_1.nested1.cell1_1.nested1",
    ]
