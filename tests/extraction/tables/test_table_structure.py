"""TableStructureRecognizer 缝的公开契约测试。

基线动机（2026-08 实测，FinTabNet.c 150 页 / 186 金标表）：pdfspine `strategy="text"` 的
表格【检出】已经够好（召回 79.6%、精确率 100%），崩掉的是检出之后的【单元格网格重建】
（仅在框对的表上 GriTS_Top 0.233）。故本缝只担「已知表格区域 → 行列网格」这一件事，
文字内容仍由调用方从 PDF 文本层按格子坐标取——绝不让模型去读字。

钉住三件事：默认关＝字节不变、确定性、以及坐标合法性不变量。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.tables.structure import (
    CellBox,
    TableRegion,
    TableStructure,
    Word,
    make_table_structure_recognizer,
)


def _region() -> TableRegion:
    """两行三列的合成表格区域（词心确定性放置，无框线）。"""
    words = []
    for row, y in enumerate((100.0, 120.0)):
        for col, x in enumerate((10.0, 60.0, 110.0)):
            words.append(Word(text=f"r{row}c{col}", bbox=(x, y, x + 30.0, y + 10.0)))
    return TableRegion(
        bbox=(0.0, 90.0, 150.0, 140.0),
        words=tuple(words),
        page_width=200.0,
        page_height=300.0,
    )


# --------------------------------------------------------------------- 工厂


def test_default_is_off_so_existing_behaviour_is_byte_identical() -> None:
    """默认 None＝关：不注入即完全不改变既有抽取路径。"""
    assert make_table_structure_recognizer() is None
    assert make_table_structure_recognizer("none") is None


def test_grid_spec_selects_deterministic_recognizer() -> None:
    recognizer = make_table_structure_recognizer("grid")

    assert recognizer is not None
    assert recognizer.name == "grid"


def test_unknown_spec_lists_available_specs() -> None:
    with pytest.raises(ValueError, match="grid"):
        make_table_structure_recognizer("telepathy")


def test_env_var_selects_recognizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGSPINE_TABLE_STRUCTURE", "grid")

    assert make_table_structure_recognizer() is not None


# ----------------------------------------------------- 确定性网格实现的行为


def test_grid_recovers_row_and_column_counts() -> None:
    structure = make_table_structure_recognizer("grid").recognize(_region())

    assert structure is not None
    assert structure.n_rows == 2
    assert structure.n_cols == 3
    assert len(structure.cells) == 6


def test_every_cell_is_addressable_exactly_once() -> None:
    structure = make_table_structure_recognizer("grid").recognize(_region())

    coords = sorted((cell.row, cell.col) for cell in structure.cells)
    assert coords == [(r, c) for r in range(2) for c in range(3)]


def test_cell_boxes_stay_inside_the_table_region() -> None:
    """坐标合法性：单元格绝不越出调用方给定的表格区域。"""
    region = _region()
    structure = make_table_structure_recognizer("grid").recognize(region)

    x0, y0, x1, y1 = region.bbox
    for cell in structure.cells:
        assert x0 <= cell.bbox[0] <= cell.bbox[2] <= x1
        assert y0 <= cell.bbox[1] <= cell.bbox[3] <= y1


def test_recognition_is_deterministic_across_calls() -> None:
    recognizer = make_table_structure_recognizer("grid")

    assert recognizer.recognize(_region()) == recognizer.recognize(_region())


def test_empty_region_yields_none_rather_than_an_empty_grid() -> None:
    """无词可依时诚实返回 None（＝没有意见），绝不编造一个空网格。"""
    empty = TableRegion(bbox=(0.0, 0.0, 10.0, 10.0), words=(), page_width=10.0, page_height=10.0)

    assert make_table_structure_recognizer("grid").recognize(empty) is None


def test_ragged_rows_do_not_crash_and_stay_rectangular() -> None:
    """行内缺格（财报表常见）不得炸，网格仍是完整矩形（缺的格给空 bbox 占位）。"""
    words = (
        Word(text="a", bbox=(10.0, 100.0, 40.0, 110.0)),
        Word(text="b", bbox=(60.0, 100.0, 90.0, 110.0)),
        Word(text="c", bbox=(10.0, 120.0, 40.0, 130.0)),
    )
    region = TableRegion(
        bbox=(0.0, 90.0, 150.0, 140.0), words=words, page_width=200.0, page_height=300.0
    )

    structure = make_table_structure_recognizer("grid").recognize(region)

    assert structure.n_rows == 2
    assert structure.n_cols == 2
    assert len(structure.cells) == 4


# ------------------------------------------------------------- 值类型不变量


def test_structure_is_frozen_and_comparable() -> None:
    cell = CellBox(row=0, col=0, row_span=1, col_span=1, bbox=(0.0, 0.0, 1.0, 1.0))
    structure = TableStructure(n_rows=1, n_cols=1, cells=(cell,))

    with pytest.raises(Exception):
        structure.n_rows = 2  # type: ignore[misc]
    assert structure == TableStructure(n_rows=1, n_cols=1, cells=(cell,))
