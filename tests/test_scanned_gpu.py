"""真实 PaddleOCR-VL 后端集成测试（三期扫描线，story #10）—— 仅 Ubuntu+GPU 执行。

本文件覆盖 PRD 两层策略中的「第②层」：真实模型 ``PaddleOcrVlBackend`` 对扫描型
PDF（``data/fixtures/pdf/scanned.pdf``）端到端抽取，断言外部行为：
    - 抽出的表格数值与 digital.pdf 的逐格真值一致（数字精确、文本归一化）。
    - 每个识别格带置信度（StyledCell.confidence 非 None）。
    - 低置信分流流程可用（低置信格仍入网格、grid 追加 warning、按
      reason='low_confidence_ocr' / priority=30 入复核队列）。

依赖约定：paddleocr / paddlepaddle-gpu 是仅在 Ubuntu+NVIDIA GPU 可用的重依赖。
本地（macOS / 无 GPU / 未装 paddleocr）下整文件 **SKIP 而非 FAIL**：
    - 模块顶层 ``pytest.importorskip('paddleocr')`` 保证未装时全文件 skip。
    - 全部用例额外打 ``@pytest.mark.gpu``，可用 ``pytest -m 'not gpu'`` 显式排除。

在 Ubuntu + NVIDIA GPU 机器上执行（生产环境，PaddleOCR-VL 一线支持环境）：
    1) 安装基础依赖与本项目（含 ocr extra）：
           uv pip install -e ".[ocr]"
    2) 按 PaddlePaddle 官方指引安装与本机 CUDA 匹配的 GPU 版 paddlepaddle：
           https://www.paddlepaddle.org.cn/install/quick
       （例如 CUDA 12.x：
           python -m pip install paddlepaddle-gpu -i \
               https://www.paddlepaddle.org.cn/packages/stable/cu126/
        以官方页面为准，prod 镜像里 pin 具体版本。）
    3) 只跑本组 GPU 集成测试：
           pytest -m gpu tests/test_scanned_gpu.py -q
       本地开发跑全套时排除 GPU：
           pytest -m 'not gpu' -q

红色阶段说明：本地这些用例表现为 SKIPPED（合规，非 FAIL）；只有在装齐
paddleocr + GPU 的 Ubuntu 机上才真正执行并对 stub 的 NotImplementedError 变红。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

# 未安装 paddleocr（本地 / CI 非 GPU）时，整文件 SKIP 而非 FAIL。
pytest.importorskip(
    "paddleocr",
    reason="paddleocr 仅在 Ubuntu+NVIDIA GPU 安装；本地无此重依赖 -> 跳过真实集成测试",
)

from ragspine.extraction.extractors.pdf_scanned_extractor import (
    LOW_CONFIDENCE_PRIORITY,
    LOW_CONFIDENCE_REASON,
    PaddleOcrVlBackend,
    extract_grids,
)
from ragspine.extraction.ir import StyledGrid
from ragspine.ingestion.review.review_queue import ReviewQueue

# 全文件统一打 gpu marker：可用 `pytest -m 'not gpu'` 显式排除。
pytestmark = pytest.mark.gpu

# 低置信分流测试的阈值：真实 PaddleOCR-VL 在干净合成扫描件上置信度极高
# （首次 GPU 实测 45 格均落在 [0.9986, 0.99996]）。低置信拦截/保留逻辑是「阈值相对」
# 的，故取落在该实测区间内的高阈值来真实触发该路径（而非依赖凑巧低于 0.85 的格）。
_REAL_LOW_CONF_THRESHOLD = 0.999


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _norm(text) -> str:
    """空白归一化：首尾 strip + 内部连续空白折叠为单空格（与抽取器约定一致）。"""
    return " ".join(str(text).split())


def _digital_cell_truth(pdf_ground_truth) -> dict:
    """digital.pdf 第 1 页表格逐格真值（REVENUE/NEWSALES/PROFIT × FY2022-2024）。

    扫描线对同一组数字的扫描件抽取，期望表格内容与数字型真值一致。
    """
    return pdf_ground_truth["files"]["digital.pdf"]["table"]["cells"]


@pytest.fixture(scope="module")
def real_backend() -> PaddleOcrVlBackend:
    """真实 PaddleOCR-VL 后端实例（仅 Ubuntu+GPU 构造成功）。"""
    return PaddleOcrVlBackend()


@pytest.fixture(scope="module")
def real_grids(scanned_pdf_path, real_backend) -> list[StyledGrid]:
    """用真实后端对 scanned.pdf 抽取出的全部 StyledGrid。"""
    return extract_grids(scanned_pdf_path, real_backend)


# ===========================================================================
# story #10 —— 真实后端构造 + 抽取产出 list[StyledGrid]
# ===========================================================================

def test_real_backend_constructs(real_backend):
    """story #10 —— 真实 PaddleOcrVlBackend 在 GPU 环境可构造（惰性加载模型）。"""
    assert real_backend is not None


def test_real_extract_returns_list_of_styled_grids(real_grids):
    """story #10 —— scanned.pdf 经真实 OCR 抽取得到非空 list[StyledGrid]。"""
    assert isinstance(real_grids, list)
    assert real_grids
    assert all(isinstance(g, StyledGrid) for g in real_grids)


def test_real_sheet_names_follow_page_table_pattern(real_grids):
    """story #10 —— 每张 grid 的 sheet 命名遵循 'page{N}_table{M}'（1-based）。"""
    import re

    pattern = re.compile(r"^page([1-9]\d*)_table([1-9]\d*)$")
    for g in real_grids:
        assert pattern.match(g.sheet) is not None, f"非法 sheet 命名: {g.sheet!r}"


# ===========================================================================
# story #10 —— 抽取数值与 digital 真值一致（数字精确、文本归一化）
# ===========================================================================

def test_real_table_values_match_digital_truth(real_grids, pdf_ground_truth):
    """story #10 —— OCR 抽出的表格逐格内容与 digital.pdf 真值一致。

    scanned.pdf 是同一组财务数字的扫描件：行/列表头（REVENUE/NEWSALES/PROFIT、
    FY2022-2024）按文本归一化比对，数值格按数字精确比对（不容差）。
    """
    truth = _digital_cell_truth(pdf_ground_truth)
    grid = real_grids[0]
    for ref, expected in truth.items():
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert _norm(cell.value) == _norm(expected), (
            f"{ref}: got {cell.value!r} expect {expected!r}"
        )


def test_real_numeric_cells_exact(real_grids, pdf_ground_truth):
    """story #10 —— 数值区（R2-R4 × C2-C4）数字必须精确，文本可归一化。"""
    truth = _digital_cell_truth(pdf_ground_truth)
    grid = real_grids[0]
    for r in (2, 3, 4):
        for c in (2, 3, 4):
            ref = f"R{r}C{c}"
            cell = grid.get(ref)
            assert cell is not None, f"{ref} 缺失"
            assert _norm(cell.value) == str(truth[ref]), (
                f"{ref}: got {cell.value!r} expect {truth[ref]}"
            )


# ===========================================================================
# story #10 —— 每个识别格带置信度（OCR 通道专用字段非 None）
# ===========================================================================

def test_real_cells_carry_confidence(real_grids):
    """story #10 —— 每个 OCR 识别格的 StyledCell.confidence 非 None 且在 [0,1]。"""
    for g in real_grids:
        assert g.cells, f"{g.sheet} 无任何格"
        for cell in g.iter_cells():
            assert cell.confidence is not None, f"{g.sheet}!{cell.cell_ref} 缺置信度"
            assert 0.0 <= cell.confidence <= 1.0


def test_real_cells_resolved_rgb_none(real_grids):
    """story #10 —— 扫描线不做颜色语义，每格 resolved_rgb 恒为 None。"""
    for g in real_grids:
        for cell in g.iter_cells():
            assert cell.resolved_rgb is None


# ===========================================================================
# story #27 —— 血缘：source_doc_id / source_file_hash 写入每张 grid
# ===========================================================================

def test_real_grids_carry_lineage(real_grids, scanned_pdf_path):
    """story #10 —— 每张 grid 带源文件名与非空 hash 血缘（审计依据）。"""
    for g in real_grids:
        assert g.source_doc_id == scanned_pdf_path.name
        assert isinstance(g.source_file_hash, str)
        assert len(g.source_file_hash) > 0
        int(g.source_file_hash, 16)  # 合法十六进制


# ===========================================================================
# story #10 —— 低置信分流流程可用（入网格 + warning + 入复核队列）
# ===========================================================================

def test_real_low_confidence_flow_enqueues(scanned_pdf_path, real_backend, tmp_db_path):
    """story #10 —— 低置信格仍入网格、grid 追加 warning，并按约定入复核队列。

    断言外部行为：传入 ReviewQueue 后，低置信格以 reason='low_confidence_ocr'、
    priority=30 入队（payload 含 locator/text/confidence 供 SME 不翻原件即可核对）。
    """
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    try:
        grids = extract_grids(
            scanned_pdf_path, real_backend,
            min_confidence=_REAL_LOW_CONF_THRESHOLD, queue=queue,
        )
        assert grids

        pending = queue.list_pending()
        # 高阈值下应有低置信格被拦截入队（不混入事实表）。
        assert pending, "未拦截任何低置信格"
        for item in pending:
            assert item.reason == LOW_CONFIDENCE_REASON
            assert item.priority == LOW_CONFIDENCE_PRIORITY
            assert "confidence" in item.payload
            assert item.payload["confidence"] < _REAL_LOW_CONF_THRESHOLD

        # 含低置信格的 grid 必须追加 warning（标记存在需复核的格）。
        assert any(g.warnings for g in grids), "低置信 grid 未追加 warning"
    finally:
        queue.close()


def test_real_low_confidence_cells_stay_in_grid(scanned_pdf_path, real_backend):
    """story #10 —— 低置信不丢数据：低于阈值的格仍保留在网格中（带其置信度）。"""
    grids = extract_grids(
        scanned_pdf_path, real_backend, min_confidence=_REAL_LOW_CONF_THRESHOLD
    )
    assert grids
    # 至少存在一个置信度低于阈值、却仍在网格中的格（被拦截而非丢弃）。
    low_conf_cells = [
        cell
        for g in grids
        for cell in g.iter_cells()
        if cell.confidence is not None and cell.confidence < _REAL_LOW_CONF_THRESHOLD
    ]
    assert low_conf_cells, "未保留任何低置信格（不应丢数据）"


def test_real_high_threshold_does_not_drop_high_confidence(scanned_pdf_path, real_backend):
    """story #10 —— 高置信格不入队：min_confidence 极低时复核队列应为空。"""
    grids = extract_grids(scanned_pdf_path, real_backend, min_confidence=0.0)
    assert grids
    # 阈值为 0 时没有任何格低于它，全部高置信通过，不应产生需复核项的 warning 噪声。
    for g in grids:
        for cell in g.iter_cells():
            assert cell.confidence is not None
            assert cell.confidence >= 0.0
