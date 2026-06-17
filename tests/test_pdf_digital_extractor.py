"""数字型 PDF 抽取器测试（二期 PDF 线，TDD 红色阶段）。

只验证外部行为：给定合成 PDF fixture 与逐格 ground truth，断言
ragspine.extraction.extractors.pdf_digital_extractor.extract_grids 的对外输出 —— 表格逐格值、
sheet/cell_ref 命名、血缘、resolved_rgb、空白归一化，以及扫描/不可读 PDF 的
返回约定。不断言 bbox 坐标等实现细节。

覆盖 PRD user stories：
    #9  数字型 PDF 的表格解析为带单元格级定位锚点的 StyledGrid（页 + 表 + 格）。
    #27 source_doc_id / source_file_hash 血缘写入每张 grid（版本/审计依据）。

红色预期：所有用例因 stub raise NotImplementedError 而 FAIL（收集成功、无 PASS）。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.extractors.pdf_digital_extractor import extract_grids
from ragspine.extraction.ir import StyledGrid


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _grids_by_sheet(path) -> dict[str, StyledGrid]:
    """把抽取出的 grid 列表按 sheet 名索引。"""
    return {g.sheet: g for g in extract_grids(path)}


def _digital_table_truth(pdf_ground_truth) -> dict:
    """digital.pdf 第 1 页表格逐格真值（含 sheet/n_rows/n_cols/cells）。"""
    return pdf_ground_truth["files"]["digital.pdf"]["table"]


# ===========================================================================
# story #9 —— 每张表一个 StyledGrid，sheet 命名 'page{N}_table{M}'
# ===========================================================================

def test_extract_returns_list_of_styled_grids(digital_pdf_path):
    """story #9 —— digital.pdf 抽取结果是 list[StyledGrid]，且至少含一张表。"""
    grids = extract_grids(digital_pdf_path)
    assert isinstance(grids, list)
    assert grids
    assert all(isinstance(g, StyledGrid) for g in grids)


def test_digital_table_sheet_named_page1_table1(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 第 1 页第 1 张表的 sheet 命名为 'page1_table1'。"""
    truth = _digital_table_truth(pdf_ground_truth)
    sheets = {g.sheet for g in extract_grids(digital_pdf_path)}
    assert truth["sheet"] in sheets
    assert truth["sheet"] == "page1_table1"


def test_table_sheet_names_follow_page_table_pattern(digital_pdf_path):
    """story #9 —— 所有 sheet 名都遵循 'page{N}_table{M}'（N、M 均为 1-based 正整数）。"""
    import re

    pattern = re.compile(r"^page([1-9]\d*)_table([1-9]\d*)$")
    for g in extract_grids(digital_pdf_path):
        m = pattern.match(g.sheet)
        assert m is not None, f"非法 sheet 命名: {g.sheet!r}"


# ===========================================================================
# story #9 —— 逐格值与 ground truth 精确一致（数值精确、文本空白归一化）
# ===========================================================================

def test_table_dimensions_match_truth(digital_pdf_path, pdf_ground_truth):
    """story #9 —— page1_table1 的逻辑行列数与真值一致（4 行 × 4 列）。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    assert grid.n_rows == truth["n_rows"]
    assert grid.n_cols == truth["n_cols"]


def test_header_cell_refs_use_R_C_notation(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 表头格用 'R{行}C{列}' 坐标（1-based），区别于 Excel 'C4' 风格。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    for ref in ("R1C2", "R1C3", "R1C4"):
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.cell_ref == ref


def _norm(text) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格（与抽取器约定一致）。"""
    return " ".join(str(text).split())


def test_row_and_col_headers_values_match(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 行/列表头文本与真值一致（容忍空白归一化）。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    # 列表头 R1C2..R1C4
    for c, expected in zip((2, 3, 4), truth["col_headers"]):
        cell = grid.get(f"R1C{c}")
        assert cell is not None
        assert _norm(cell.value) == _norm(expected)
    # 行表头 R2C1..R4C1
    for r, expected in zip((2, 3, 4), truth["row_headers"]):
        cell = grid.get(f"R{r}C1")
        assert cell is not None
        assert _norm(cell.value) == _norm(expected)


def test_numeric_values_match_exactly(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 数值格的数字必须精确（文本可归一化、数字不容差）。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    # 数值区：R2..R4 × C2..C4
    for r in (2, 3, 4):
        for c in (2, 3, 4):
            ref = f"R{r}C{c}"
            expected = truth["cells"][ref]  # ground truth 中为 int
            cell = grid.get(ref)
            assert cell is not None, f"{ref} 缺失"
            # 抽取器单元格文本归一化后应等于真值数字的字符串形式（数字精确）
            assert _norm(cell.value) == str(expected), (
                f"{ref}: got {cell.value!r} expect {expected}"
            )


def test_all_truth_cells_present_and_match(digital_pdf_path, pdf_ground_truth):
    """story #9 —— ground truth 逐格真值全量比对（每格存在、值归一化后一致）。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    for ref, expected in truth["cells"].items():
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.cell_ref == ref
        assert _norm(cell.value) == _norm(expected), (
            f"{ref}: got {cell.value!r} expect {expected!r}"
        )


def test_top_left_corner_cell_is_blank_or_absent(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 表头行左上角（R1C1）真值不存在：要么缺格，要么值归一化为空。

    边界：稀疏映射只存有内容的格，左上角空白不得凭空捏造出非空文本。
    """
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    assert "R1C1" not in truth["cells"]
    cell = grid.get("R1C1")
    if cell is not None:
        assert _norm(cell.value) == ""


# ===========================================================================
# story #9 —— 单元格文本空白归一化（不留首尾空白/不残留多空格）
# ===========================================================================

def test_cell_text_is_whitespace_normalized(digital_pdf_path, pdf_ground_truth):
    """story #9 —— 文本格不带首尾空白、内部无连续多空白（已归一化）。"""
    truth = _digital_table_truth(pdf_ground_truth)
    grid = _grids_by_sheet(digital_pdf_path)[truth["sheet"]]
    for cell in grid.iter_cells():
        text = cell.value
        if not isinstance(text, str):
            continue
        assert text == text.strip(), f"首尾空白未归一化: {text!r}"
        assert "  " not in text, f"内部连续空白未折叠: {text!r}"


# ===========================================================================
# story #9 —— resolved_rgb 一律 None（PDF 不做颜色语义）
# ===========================================================================

def test_all_cells_resolved_rgb_none(digital_pdf_path):
    """story #9 —— 抽出的每个单元格 resolved_rgb 恒为 None（颜色语义是 Excel/PPT 的范畴）。"""
    grids = extract_grids(digital_pdf_path)
    assert grids
    for g in grids:
        for cell in g.iter_cells():
            assert cell.resolved_rgb is None


# ===========================================================================
# story #27 —— 血缘：source_doc_id / source_file_hash 写入每张 grid
# ===========================================================================

def test_grids_carry_source_doc_id(digital_pdf_path):
    """story #27 —— 每张 grid 的 source_doc_id 为源文件名（下游血缘根）。"""
    grids = extract_grids(digital_pdf_path)
    assert grids
    for g in grids:
        assert g.source_doc_id == digital_pdf_path.name


def test_grids_carry_nonempty_source_hash(digital_pdf_path):
    """story #27 —— 每张 grid 的 source_file_hash 为非空十六进制串。"""
    grids = extract_grids(digital_pdf_path)
    assert grids
    for g in grids:
        assert isinstance(g.source_file_hash, str)
        assert len(g.source_file_hash) > 0
        int(g.source_file_hash, 16)  # 必须是合法十六进制


def test_grids_share_same_source_hash(digital_pdf_path):
    """story #27 —— 同一文件抽出的所有 grid 共享同一个 source_file_hash（确定性血缘）。"""
    hashes = {g.source_file_hash for g in extract_grids(digital_pdf_path)}
    assert len(hashes) == 1


# ===========================================================================
# 扫描 / 不可读 PDF —— 返回 [] 不抛异常（依赖分诊路由）
# ===========================================================================

def test_scanned_pdf_returns_empty(scanned_pdf_path):
    """story #9 —— 扫描型 PDF（无文本层）输入返回 []，不抛异常、不做 OCR。"""
    out = extract_grids(scanned_pdf_path)
    assert out == []


def test_ocr_scan_pdf_returns_empty(ocr_scan_pdf_path):
    """story #9 —— OCR 扫描型 PDF（位图 + 隐形文本层）仍按扫描处理，返回 []。

    刁钻：隐形文本层存在但视觉是位图，不应被误当数字表格抽取。
    """
    out = extract_grids(ocr_scan_pdf_path)
    assert out == []


def test_scanned_pdf_returns_list_type(scanned_pdf_path):
    """story #9 —— 扫描型 PDF 返回值类型仍是 list（空列表而非 None）。"""
    assert isinstance(extract_grids(scanned_pdf_path), list)


# ===========================================================================
# 路径入参鲁棒性 + 叙述文本不入表
# ===========================================================================

def test_accepts_str_and_path_equivalently(digital_pdf_path, pdf_ground_truth):
    """story #9 —— str 与 Path 入参产出一致的 sheet 集合（接口宽容）。"""
    by_path = {g.sheet for g in extract_grids(digital_pdf_path)}
    by_str = {g.sheet for g in extract_grids(str(digital_pdf_path))}
    assert by_path == by_str
    assert _digital_table_truth(pdf_ground_truth)["sheet"] in by_path


def test_narrative_text_not_emitted_as_table(digital_pdf_path):
    """story #9 —— 第 2 页纯叙述文本不产出表格 grid（只产表格网格，正文不在范围）。

    刁钻：digital.pdf 第 2 页是一段散文，不得被解析成 page2_table* 之类的网格。
    """
    sheets = {g.sheet for g in extract_grids(digital_pdf_path)}
    assert not any(s.startswith("page2_table") for s in sheets)
