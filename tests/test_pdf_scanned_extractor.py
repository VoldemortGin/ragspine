"""扫描型 PDF OCR 抽取器测试（三期扫描线，TDD 红色阶段）。

只验证外部行为：用测试文件内自定义的 FakeOcrBackend（依赖注入，按 pptx ground truth
的 OCR 测试向量返回确定性 OcrPageResult）驱动 ragspine.extraction.extractors.pdf_scanned_extractor
.extract_grids，断言其对外输出 —— 网格逐格值 / cell_ref / sheet 命名 / 置信度落点、
低置信分流（仍入网格 + grid warning + 复核入队）、min_confidence 阈值可调、不给 queue
零副作用、空文件 / 不可读 → []、backend 被逐页调用且页号正确。

绝不真正 import paddle/paddleocr：FakeOcrBackend 完全替身，本地无 GPU 也可跑（PRD 两层
策略的第①层「模型无关」逻辑）。

覆盖 PRD user stories：
    #10 扫描型 PDF 经 OCR 抽取且每个值带置信度，低置信结果被自动拦截入复核而非混入事实表。
    #22 低置信项进入人工复核队列（reason / priority / payload 定位）。
    #27 source_doc_id / source_file_hash 血缘写入每张 grid。

红色预期：除空文件 / 不可读路径等「契约即返回 []」的分支外，所有断行为入口因 stub
raise NotImplementedError 而 FAIL（收集成功、无意外 PASS）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.extractors.pdf_scanned_extractor import (
    LOW_CONFIDENCE_PRIORITY,
    LOW_CONFIDENCE_REASON,
    OcrCell,
    OcrPageResult,
    OcrTable,
    extract_grids,
)
from ragspine.extraction.ir import StyledGrid
from ragspine.ingestion.review.review_queue import ReviewQueue


# ---------------------------------------------------------------------------
# 测试专用 fake backend（定义在测试文件内，不放 src）
# ---------------------------------------------------------------------------

class FakeOcrBackend:
    """实现 OcrBackend 协议的确定性替身。

    按 pptx ground truth 的 OCR 向量（ocr_fake.pages）返回固定的 OcrPageResult，
    并记录每次被调用的页号，供「逐页调用 + 页号正确」断言使用。绝不 import paddle。
    """

    def __init__(self, ocr_fake: dict):
        # page_no -> OcrPageResult，按 ground truth 向量预构造。
        self._by_page: dict[int, OcrPageResult] = {}
        for page in ocr_fake["pages"]:
            tables = [
                OcrTable(
                    n_rows=t["n_rows"],
                    n_cols=t["n_cols"],
                    cells=[
                        OcrCell(
                            row=c["row"],
                            col=c["col"],
                            text=c["text"],
                            confidence=c["confidence"],
                        )
                        for c in t["cells"]
                    ],
                )
                for t in page["tables"]
            ]
            self._by_page[page["page_no"]] = OcrPageResult(
                page_no=page["page_no"],
                tables=tables,
                warnings=list(page.get("warnings", [])),
            )
        # 调用记录：每次 recognize 被调用时追加 (page_no_arg, image_is_bytes)。
        self.calls: list[tuple[int, bool]] = []

    def recognize(self, image_bytes: bytes, page_no: int) -> OcrPageResult:
        self.calls.append((page_no, isinstance(image_bytes, (bytes, bytearray))))
        return self._by_page[page_no]


# ---------------------------------------------------------------------------
# fixtures / 辅助
# ---------------------------------------------------------------------------

@pytest.fixture
def ocr_fake(pptx_ground_truth) -> dict:
    """OCR fake 测试向量（内嵌在 pptx ground truth 的 ocr_fake 段）。"""
    return pptx_ground_truth["ocr_fake"]


@pytest.fixture
def fake_backend(ocr_fake) -> FakeOcrBackend:
    """按 ground truth 向量构造的确定性 fake backend。"""
    return FakeOcrBackend(ocr_fake)


def _grids_by_sheet(path, backend, **kwargs) -> dict[str, StyledGrid]:
    """把抽取出的 grid 列表按 sheet 名索引。"""
    return {g.sheet: g for g in extract_grids(path, backend, **kwargs)}


# ===========================================================================
# story #10 —— 每张 OcrTable 一个 StyledGrid，sheet 命名 'page{N}_table{M}'
# ===========================================================================

def test_returns_list_of_styled_grids(scanned_pdf_path, fake_backend):
    """story #10 —— scanned.pdf 经 fake backend 抽取，结果是非空 list[StyledGrid]。"""
    grids = extract_grids(scanned_pdf_path, fake_backend)
    assert isinstance(grids, list)
    assert grids
    assert all(isinstance(g, StyledGrid) for g in grids)


def test_one_grid_per_page_table(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 3 页各 1 张表 -> 恰好 3 张 StyledGrid。"""
    grids = extract_grids(scanned_pdf_path, fake_backend)
    expected = len(ocr_fake["expected_grids"])
    assert len(grids) == expected == 3


def test_sheet_names_follow_page_table_pattern(scanned_pdf_path, fake_backend):
    """story #10 —— 每张 grid 的 sheet 名遵循 'page{N}_table{M}'（N、M 均 1-based）。"""
    import re

    pattern = re.compile(r"^page([1-9]\d*)_table([1-9]\d*)$")
    for g in extract_grids(scanned_pdf_path, fake_backend):
        assert pattern.match(g.sheet) is not None, f"非法 sheet 命名: {g.sheet!r}"


def test_expected_sheet_names_present(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 三页表的 sheet 名恰为 page1_table1 / page2_table1 / page3_table1。"""
    sheets = {g.sheet for g in extract_grids(scanned_pdf_path, fake_backend)}
    expected = {eg["sheet"] for eg in ocr_fake["expected_grids"]}
    assert sheets == expected
    assert "page1_table1" in sheets


# ===========================================================================
# story #10 —— 逐格值 / cell_ref / 维度与 ground truth 精确一致
# ===========================================================================

def test_grid_dimensions_match_truth(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 每张 grid 的 n_rows/n_cols 与向量维度一致（4×4）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        grid = by_sheet[eg["sheet"]]
        assert grid.n_rows == eg["n_rows"]
        assert grid.n_cols == eg["n_cols"]


def test_cell_refs_use_R_C_notation(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 单元格坐标用 'R{行}C{列}'（1-based），cell_ref 自洽。"""
    grid = _grids_by_sheet(scanned_pdf_path, fake_backend)[
        ocr_fake["expected_grids"][0]["sheet"]
    ]
    for ref in ocr_fake["cell_values"]:
        cell = grid.get(ref)
        assert cell is not None, f"{ref} 缺失"
        assert cell.cell_ref == ref


def test_cell_values_match_truth(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 每页 grid 逐格文本与 OCR 向量真值精确一致（含低置信格的值）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        grid = by_sheet[eg["sheet"]]
        for ref, expected in ocr_fake["cell_values"].items():
            cell = grid.get(ref)
            assert cell is not None, f"{eg['sheet']}!{ref} 缺失"
            assert cell.value == expected, (
                f"{eg['sheet']}!{ref}: got {cell.value!r} expect {expected!r}"
            )


def test_n_cells_match_truth(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 每张 grid 的有内容格数与向量一致（稀疏：左上角空格不入网格，15 格）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        grid = by_sheet[eg["sheet"]]
        assert len(list(grid.iter_cells())) == eg["n_cells"]
        assert eg["n_cells"] == 15


def test_resolved_rgb_always_none(scanned_pdf_path, fake_backend):
    """story #10 —— 扫描线不做颜色语义：每个单元格 resolved_rgb 恒为 None。"""
    for g in extract_grids(scanned_pdf_path, fake_backend):
        for cell in g.iter_cells():
            assert cell.resolved_rgb is None


# ===========================================================================
# story #10 —— 置信度落在 StyledCell.confidence（OCR 通道专用字段）
# ===========================================================================

def test_high_confidence_lands_on_cell(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 高置信格的 OCR 置信度写入 StyledCell.confidence（如 0.99）。"""
    high = ocr_fake["high_confidence_value"]
    grid = _grids_by_sheet(scanned_pdf_path, fake_backend)[
        ocr_fake["expected_grids"][0]["sheet"]
    ]
    cell = grid.get("R2C3")  # 向量中为高置信格
    assert cell is not None
    assert cell.confidence == high


def test_low_confidence_lands_on_cell(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 低置信格的置信度同样写入 StyledCell.confidence（如 0.40）。"""
    low = ocr_fake["low_confidence_value"]
    grid = _grids_by_sheet(scanned_pdf_path, fake_backend)[
        ocr_fake["expected_grids"][0]["sheet"]
    ]
    for ref in ocr_fake["expected_grids"][0]["low_confidence_refs"]:
        cell = grid.get(ref)
        assert cell is not None, f"低置信格 {ref} 不应被丢弃"
        assert cell.confidence == low


def test_every_cell_carries_confidence(scanned_pdf_path, fake_backend):
    """story #10 —— OCR 通道每个格都带数值置信度（不为 None，区别于确定性抽取）。"""
    for g in extract_grids(scanned_pdf_path, fake_backend):
        for cell in g.iter_cells():
            assert isinstance(cell.confidence, float)


# ===========================================================================
# story #10 —— 低置信格仍入网格，但 grid 追加 warning
# ===========================================================================

def test_low_confidence_cells_stay_in_grid(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 低置信不丢数据：低置信格仍出现在网格中（仅标记，不删除）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        grid = by_sheet[eg["sheet"]]
        for ref in eg["low_confidence_refs"]:
            assert grid.get(ref) is not None, f"{eg['sheet']}!{ref} 被错误丢弃"


def test_grid_with_low_confidence_has_warning(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 含低置信格的 grid 至少追加一条 warning（标记存在低置信格）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        if eg["expect_warning"]:
            grid = by_sheet[eg["sheet"]]
            assert len(grid.warnings) >= 1, f"{eg['sheet']} 缺 low-confidence warning"


# ===========================================================================
# story #10 / #22 —— 给 queue 时低置信格入队（reason / priority / payload）
# ===========================================================================

def test_low_confidence_enqueued_when_queue_given(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #22 —— 传入 ReviewQueue 时，低置信格按总数入队（3 页 × 2 格 = 6）。"""
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    extract_grids(scanned_pdf_path, fake_backend, queue=queue)
    pending = queue.list_pending()
    assert len(pending) == ocr_fake["expected_total_enqueued"] == 6
    queue.close()


def test_enqueued_items_reason_and_priority(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #22 —— 入队项 reason='low_confidence_ocr'、priority=30（与契约常量一致）。"""
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    extract_grids(scanned_pdf_path, fake_backend, queue=queue)
    for item in queue.list_pending():
        assert item.reason == ocr_fake["enqueue_reason"] == LOW_CONFIDENCE_REASON
        assert item.priority == ocr_fake["enqueue_priority"] == LOW_CONFIDENCE_PRIORITY
    queue.close()


def test_enqueued_payload_has_locator_text_confidence(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #22 —— 入队项 payload 含 locator / text / confidence，SME 不翻原件即可核对。

    locator 须能定位回原格（含 sheet 与 cell_ref），text 为识别文本，confidence 为低置信值。
    """
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    extract_grids(scanned_pdf_path, fake_backend, queue=queue)

    expected_sheets = {eg["sheet"] for eg in ocr_fake["expected_grids"]}
    low_refs = set(ocr_fake["expected_grids"][0]["low_confidence_refs"])
    low_value = ocr_fake["low_confidence_value"]

    for item in queue.list_pending():
        payload = item.payload
        assert "locator" in payload
        assert "text" in payload
        assert "confidence" in payload
        # locator 能回链到某张表的某个低置信格。
        assert any(s in payload["locator"] for s in expected_sheets)
        assert any(ref in payload["locator"] for ref in low_refs)
        # 低置信入队 -> 置信度小于阈值。
        assert payload["confidence"] == low_value
        assert payload["confidence"] < ocr_fake["min_confidence"]
        # text 为该格的真值文本。
        assert payload["text"] == ocr_fake["cell_values"][_ref_in(payload["locator"], low_refs)]
    queue.close()


def _ref_in(locator: str, refs: set[str]) -> str:
    """从 locator 串里取出命中的低置信 cell_ref（辅助断言用）。"""
    for ref in refs:
        if ref in locator:
            return ref
    raise AssertionError(f"locator {locator!r} 未含任何低置信 cell_ref {refs}")


def test_high_confidence_not_enqueued(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #22 —— 高置信格不入队（仅低置信被拦截，避免淹没复核队列）。"""
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    extract_grids(scanned_pdf_path, fake_backend, queue=queue)
    high = ocr_fake["high_confidence_value"]
    for item in queue.list_pending():
        assert item.payload["confidence"] != high
        assert item.payload["confidence"] < ocr_fake["min_confidence"]
    queue.close()


# ===========================================================================
# story #10 —— min_confidence 阈值可调（调低后低置信格不再触发拦截）
# ===========================================================================

def test_lower_threshold_disables_enqueue(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #10 —— 把 min_confidence 调到低于全部置信度后，无格入队（阈值可调）。"""
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    # 0.40 是向量里最低的置信度，阈值设 0.0 则没有格低于阈值。
    extract_grids(scanned_pdf_path, fake_backend, min_confidence=0.0, queue=queue)
    assert queue.list_pending() == []
    queue.close()


def test_lower_threshold_no_grid_warning(scanned_pdf_path, fake_backend):
    """story #10 —— 阈值调到 0.0 后，grid 不再追加 low-confidence warning。"""
    grids = extract_grids(scanned_pdf_path, fake_backend, min_confidence=0.0)
    for g in grids:
        assert g.warnings == []


def test_higher_threshold_enqueues_more(
    scanned_pdf_path, fake_backend, ocr_fake, tmp_db_path
):
    """story #10 —— 把阈值抬到高于全部置信度后，每张表所有格都被拦截入队（阈值双向可调）。"""
    queue = ReviewQueue(tmp_db_path)
    queue.init_schema()
    extract_grids(scanned_pdf_path, fake_backend, min_confidence=1.0, queue=queue)
    total_cells = sum(eg["n_cells"] for eg in ocr_fake["expected_grids"])
    assert len(queue.list_pending()) == total_cells
    queue.close()


# ===========================================================================
# story #10 —— 不给 queue 零副作用（仍正常出网格，含低置信标记）
# ===========================================================================

def test_no_queue_no_error(scanned_pdf_path, fake_backend):
    """story #10 —— 不传 queue 时不抛异常，仍正常返回网格（队列是可选副作用）。"""
    grids = extract_grids(scanned_pdf_path, fake_backend)
    assert grids


def test_no_queue_still_marks_low_confidence(scanned_pdf_path, fake_backend, ocr_fake):
    """story #10 —— 不传 queue 时低置信格仍入网格 + grid warning（拦截标记独立于入队）。"""
    by_sheet = _grids_by_sheet(scanned_pdf_path, fake_backend)
    for eg in ocr_fake["expected_grids"]:
        grid = by_sheet[eg["sheet"]]
        for ref in eg["low_confidence_refs"]:
            assert grid.get(ref) is not None
        if eg["expect_warning"]:
            assert len(grid.warnings) >= 1


# ===========================================================================
# story #27 —— 血缘：source_doc_id / source_file_hash 写入每张 grid
# ===========================================================================

def test_grids_carry_source_doc_id(scanned_pdf_path, fake_backend):
    """story #27 —— 每张 grid 的 source_doc_id 为源文件名（下游血缘根）。"""
    for g in extract_grids(scanned_pdf_path, fake_backend):
        assert g.source_doc_id == scanned_pdf_path.name


def test_grids_share_nonempty_source_hash(scanned_pdf_path, fake_backend):
    """story #27 —— 所有 grid 共享同一非空 source_file_hash（确定性内容血缘）。"""
    hashes = {g.source_file_hash for g in extract_grids(scanned_pdf_path, fake_backend)}
    assert len(hashes) == 1
    only = next(iter(hashes))
    assert isinstance(only, str) and only


# ===========================================================================
# story #10 —— backend 被逐页调用，且页号正确（1-based、按页序）
# ===========================================================================

def test_backend_called_once_per_page(scanned_pdf_path, fake_backend):
    """story #10 —— 3 页 PDF -> backend.recognize 恰好被调用 3 次（逐页渲染识别）。"""
    extract_grids(scanned_pdf_path, fake_backend)
    assert len(fake_backend.calls) == 3


def test_backend_called_with_correct_page_numbers(scanned_pdf_path, fake_backend):
    """story #10 —— recognize 的 page_no 为 1-based 且按页序覆盖全部页（[1,2,3]）。"""
    extract_grids(scanned_pdf_path, fake_backend)
    page_nos = [pn for pn, _ in fake_backend.calls]
    assert page_nos == [1, 2, 3]


def test_backend_receives_image_bytes(scanned_pdf_path, fake_backend):
    """story #10 —— 每次 recognize 收到的是渲染出的 PNG bytes（图像入参，非路径）。"""
    extract_grids(scanned_pdf_path, fake_backend)
    assert fake_backend.calls
    assert all(is_bytes for _, is_bytes in fake_backend.calls)


# ===========================================================================
# 边界 —— 空文件 / 不可读 PDF 返回 []（不抛异常、不调用 backend）
# ===========================================================================

def test_empty_file_returns_empty(tmp_path, fake_backend):
    """story #10 —— 零字节 / 空 PDF 文件输入返回 []，不抛异常。"""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert extract_grids(empty, fake_backend) == []


def test_unreadable_file_returns_empty(tmp_path, fake_backend):
    """story #10 —— 损坏 / 不可读（非 PDF 内容）文件返回 []，不抛异常。"""
    junk = tmp_path / "broken.pdf"
    junk.write_bytes(b"this is not a valid pdf payload at all")
    assert extract_grids(junk, fake_backend) == []


def test_unreadable_file_does_not_call_backend(tmp_path):
    """story #10 —— 不可读输入不应触发任何 OCR 调用（先分诊、后识别）。"""
    junk = tmp_path / "broken2.pdf"
    junk.write_bytes(b"not a pdf")
    spy = FakeOcrBackend({"pages": []})
    out = extract_grids(junk, spy)
    assert out == []
    assert spy.calls == []
