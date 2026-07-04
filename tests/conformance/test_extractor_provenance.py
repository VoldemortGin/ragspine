"""Extractor 抽取处 provenance 不变量绑定（conformance，over real fixtures）。

落地 docs/prd-breadth-via-adapters.md「Invariant-binding conformance kit · Provenance
(SourceConnector, Extractor, Chunker): every emitted unit carries a non-null source_doc_id +
locator; lineage survives the transform. An adapter that drops it fails.」把 provenance 绑死在
Extractor 缝上，对【每个注册 extractor】经其真实/合成最小 fixture 参数化断言——任何抽取器
（内置 xlsx/pptx/pdf/docx，含未来第三方格式）只要登记进 conftest.EXTRACTOR_IMPLS 就必须证明
每张 emitted StyledGrid 带非空 source_doc_id（血缘根）与 sheet + 每格 cell_ref（locator，下游
source_locator='sheet=…!R{r}C{c}' 的 citation 回指构件），lineage 经抽取存活。在抽取处丢血缘的
实现直接 CI 红，而非生产事故。

非空泛证明（同 SourceConnector / Chunker / FactStore / VectorStore 的「诚实反证」手法）：故意丢
血缘的 stub extractor 喂进同一断言核【必须 FAIL】——两支反证分别抹掉 source_doc_id（血缘根）与
cell_ref（locator），证明断言在【根】与【定位】两个维度都非空泛通过。
"""

import os
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


def _assert_extractor_carries_provenance(extractor, path) -> int:
    """对一个 Extractor 断言：产出非空，且每张 StyledGrid（及其每个 emitted cell）都带齐血缘。

    逐张 StyledGrid 断言：
      - source_doc_id 非空（血缘根，对应 fact_metric.source_doc_id）。
      - sheet 非空（粗粒度 locator：page{N}_table{M} / slide{N}_table{M} / table{M} / 工作表名）。
      - 至少一格；每个 emitted StyledCell 的 cell_ref 非空（细粒度 locator R{r}C{c} / C4，citation 回指）。
      - 若写了 source_file_hash（版本/审计血缘），不得为空串。

    返回产出的 StyledGrid 数。这是 provenance pack 的【单一判定核】——参数化用例与「反证 stub」
    共用它，确保两者验的是同一条不变量。
    """
    grids = extractor.extract(path)
    assert grids, f"extractor 未产出任何 StyledGrid（fixture 非空，应至少抽出一张表）：{path!r}"
    for g in grids:
        assert g.source_doc_id, f"StyledGrid 缺 source_doc_id（血缘根）：{g!r}"
        assert g.sheet, f"StyledGrid 缺 sheet（粗粒度 locator）：{g!r}"
        if g.source_file_hash is not None:
            assert g.source_file_hash, f"StyledGrid 的 source_file_hash 为空串（血缘存活失败）：{g!r}"
        cells = list(g.iter_cells())
        assert cells, f"StyledGrid 无任何单元格（fixture 表非空，应至少一格）：{g.sheet!r}"
        for cell in cells:
            assert cell.cell_ref, (
                f"StyledCell 缺 cell_ref（locator，citation 回指）：{cell!r} @ {g.sheet!r}"
            )
    return len(grids)


# ===========================================================================
# P · Provenance：每个注册 Extractor 的产出都带齐血缘（over real / 合成最小 fixtures）
# ===========================================================================

def test_every_grid_carries_provenance(extractor_case):
    """每个注册 Extractor：抽出的每张 StyledGrid（及其每格）都带非空 source_doc_id + locator。"""
    extractor, path = extractor_case
    n = _assert_extractor_carries_provenance(extractor, path)
    assert n >= 1  # fixture 表非空，应至少抽出一张 StyledGrid


def test_source_doc_id_is_filename(extractor_case):
    """血缘根口径：source_doc_id = 源文件名（与 fact_metric.source_doc_id / narrative doc_id 一致）。"""
    extractor, path = extractor_case
    ids = {g.source_doc_id for g in extractor.extract(path)}
    assert ids == {Path(path).name}


def test_provenance_stable_across_calls(extractor_case):
    """确定性血缘：同一 extractor 两次抽取产出相同的 (source_doc_id, sheet) 序列（顺序稳定）。"""
    extractor, path = extractor_case
    first = [(g.source_doc_id, g.sheet) for g in extractor.extract(path)]
    second = [(g.source_doc_id, g.sheet) for g in extractor.extract(path)]
    assert first == second


def test_extractor_is_runtime_checkable(extractor_case):
    """每个注册 Extractor 都结构匹配 @runtime_checkable Extractor Protocol（extract(path)）。"""
    from ragspine.extraction.registry import Extractor

    extractor, _ = extractor_case
    assert isinstance(extractor, Extractor)


# ===========================================================================
# 非空泛证明：丢血缘的 stub 必须 FAIL（证明 provenance pack 在【根】与【定位】两维都非空泛）
# ===========================================================================

class _LineageDroppingExtractor:
    """反证 stub：产出 source_doc_id 抹空的 StyledGrid——【故意】丢血缘根。"""

    def extract(self, path):
        from ragspine.extraction.ir import StyledCell, StyledGrid

        grid = StyledGrid(sheet="page1_table1", source_doc_id="")  # 血缘根抹空
        grid.cells["R1C1"] = StyledCell(value="4500", cell_ref="R1C1")
        return [grid]


class _LocatorDroppingExtractor:
    """反证 stub：产出 cell_ref 抹空的 StyledCell——【故意】丢 locator（citation 回指断裂）。"""

    def extract(self, path):
        from ragspine.extraction.ir import StyledCell, StyledGrid

        grid = StyledGrid(sheet="page1_table1", source_doc_id="memo.pdf")
        grid.cells["R1C1"] = StyledCell(value="4500", cell_ref="")  # locator 抹空
        return [grid]


def test_lineage_dropping_stub_fails_provenance():
    """喂丢血缘根 stub 进同一断言核必须 AssertionError——证明 provenance pack 非空泛（根维度）。"""
    with pytest.raises(AssertionError):
        _assert_extractor_carries_provenance(_LineageDroppingExtractor(), "memo.pdf")


def test_locator_dropping_stub_fails_provenance():
    """喂丢 locator stub 进同一断言核必须 AssertionError——证明 provenance pack 非空泛（定位维度）。"""
    with pytest.raises(AssertionError):
        _assert_extractor_carries_provenance(_LocatorDroppingExtractor(), "memo.pdf")
