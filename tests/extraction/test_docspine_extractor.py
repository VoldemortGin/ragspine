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
