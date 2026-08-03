"""TableStructureRecognizer 缝的参数化 conformance：所有实现跑同一套不变量。

与家族其他缝（VectorStore / GraphStore / TraceSink）同款：把不变量绑在缝上而非某个实现上，
任何新后端接进来必须原样通过本文件，否则不算实现了这条缝。

三项不变量：
- **确定性** —— 同输入同输出，跨实例逐位一致。
- **坐标合法性** —— 单元格 bbox 绝不越出调用方给定的表格区域；x0<=x1、y0<=y1。
- **诚实降级** —— 无法判断时返回 None（＝没有意见），绝不编造空网格或部分网格。

视觉后端（tatr）需 `[tsr]` extra，CI 精简门默认不装，故经 importorskip 跳过。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.tables.structure import (
    TableRegion,
    Word,
    make_table_structure_recognizer,
)


def _make(spec: str):
    if spec == "tatr":
        pytest.importorskip("transformers", reason="视觉 TSR 后端需 [tsr] extra")
        pytest.importorskip("torch", reason="视觉 TSR 后端需 [tsr] extra")
    return make_table_structure_recognizer(spec)


SPECS = ["grid", "tatr"]


def _region(words: tuple[Word, ...] = ()) -> TableRegion:
    if not words:
        words = tuple(
            Word(
                text=f"r{r}c{c}",
                bbox=(10.0 + 50 * c, 100.0 + 20 * r, 40.0 + 50 * c, 110.0 + 20 * r),
            )
            for r in range(3)
            for c in range(4)
        )
    return TableRegion(
        bbox=(0.0, 90.0, 220.0, 170.0),
        words=words,
        page_width=300.0,
        page_height=400.0,
    )


@pytest.mark.parametrize("spec", SPECS)
def test_recognition_is_deterministic(spec: str) -> None:
    recognizer = _make(spec)
    region = _region()

    first = recognizer.recognize(region)
    second = _make(spec).recognize(region)

    assert first == second


@pytest.mark.parametrize("spec", SPECS)
def test_cells_stay_inside_the_region(spec: str) -> None:
    recognizer = _make(spec)
    region = _region()

    structure = recognizer.recognize(region)
    if structure is None:
        pytest.skip("实现对该输入无意见（合法的诚实降级）")

    x0, y0, x1, y1 = region.bbox
    for cell in structure.cells:
        cx0, cy0, cx1, cy1 = cell.bbox
        assert cx0 <= cx1 and cy0 <= cy1, "bbox 必须是正向矩形"
        assert x0 <= cx0 and cx1 <= x1, "单元格不得越出表格区域（横向）"
        assert y0 <= cy0 and cy1 <= y1, "单元格不得越出表格区域（纵向）"


@pytest.mark.parametrize("spec", SPECS)
def test_grid_is_rectangular_and_fully_addressed(spec: str) -> None:
    recognizer = _make(spec)

    structure = recognizer.recognize(_region())
    if structure is None:
        pytest.skip("实现对该输入无意见（合法的诚实降级）")

    coords = sorted((cell.row, cell.col) for cell in structure.cells)
    expected = [(r, c) for r in range(structure.n_rows) for c in range(structure.n_cols)]
    assert coords == expected
    assert all(cell.row_span >= 1 and cell.col_span >= 1 for cell in structure.cells)


@pytest.mark.parametrize("spec", SPECS)
def test_empty_input_degrades_honestly_to_none(spec: str) -> None:
    recognizer = _make(spec)

    empty = TableRegion(bbox=(0.0, 0.0, 10.0, 10.0), words=(), page_width=10.0, page_height=10.0)

    assert recognizer.recognize(empty) is None


@pytest.mark.parametrize("spec", SPECS)
def test_recognizer_satisfies_the_protocol(spec: str) -> None:
    from ragspine.extraction.tables.structure import TableStructureRecognizer

    recognizer = _make(spec)

    assert isinstance(recognizer, TableStructureRecognizer)
    assert isinstance(recognizer.name, str) and recognizer.name
