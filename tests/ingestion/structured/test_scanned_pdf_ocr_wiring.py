"""扫描型 PDF OCR 接通入库主路径的测试（W3a，TDD 红色阶段）。

W3a 缺口（docs/prd-quality-depth.md）：scanned-PDF OCR 缝早已建好并测过，却从未被调用——
`_ingest_pdf` 对 scanned/ocr_scan/mixed 判定【只入复核队列】，从不调用 pdf_scanned_extractor，
故扫描型 PDF 永远不会被 OCR、永远进不了检索。本批接通：这类判定【实际调用】家族 OCR
（pdf_scanned_extractor.extract_grids，默认 PdfSpineOcrBackend）产出 StyledGrid → 结构化通路，
让扫描件真正被 OCR 成 facts 并可检索；低置信格仍路由复核；provenance 不丢。

只验证外部行为：注入【确定性 fake OCR 后端】（模型无关，本地无 GPU / 无 pdfspine 亦可跑，
沿用 PRD 两层测试策略第①层），断言 ingest_file 把扫描 PDF 的 OCR 结果落库且可 query 回来、
低置信格进复核、血缘完整、dry_run 零写入、幂等。默认走家族 OCR 由独立用例（spy 捕获后端类型）
证明，无需真跑 OCR。

红色预期：用例因 `ingest_file(..., ocr_backend=...)` 关键字未实现（TypeError）/ 扫描分支仍只
enqueue 而 FAIL。import 放在测试体内首行，使其作为 FAILURE 而非 collection ERROR 暴露
（沿用 tests/ingestion/test_ingest_dispatch.py 的红色阶段约定）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.extraction.extractors.pdf_scanned_extractor import (
    LOW_CONFIDENCE_REASON,
    OcrCell,
    OcrPageResult,
    OcrTable,
)
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.storage.fact_store import VISIBLE_REVIEW_STATUSES, FactStore

# 受控实体（home 公司 profile 可归一）：'ACME Hong Kong' -> ACME_HK。
RESOLVABLE_ENTITY_TITLE = "ACME Hong Kong"
HIGH_CONF = 0.99
LOW_CONF = 0.40  # < 默认 min_confidence(0.85) -> 路由复核


class _FakeOcrBackend:
    """确定性 OcrBackend 替身：每页返回同一张实体可解析的财务表（含一个低置信格）。

    表布局（与 xlsx/pptx 财务表同套「R1C1 实体 / 首行期间 / 首列指标」约定）：
        R1C1='ACME Hong Kong'  R1C2='FY2024'
        R2C1='REVENUE'         R2C2='2680'(高置信)
        R3C1='PROFIT'          R3C2='2210'(低置信 -> 复核)
    携带 version 以验证血缘 extractor_version 随后端而定（与 GridExtractor.version 同范式）。
    绝不 import pdfspine / paddle。
    """

    version = "fake_ocr@1"

    def __init__(self, *, empty: bool = False) -> None:
        self._empty = empty
        self.calls: list[int] = []

    def recognize(self, image_bytes: bytes, page_no: int) -> OcrPageResult:
        self.calls.append(page_no)
        if self._empty:
            return OcrPageResult(page_no=page_no, tables=[], warnings=["无表格"])
        cells = [
            OcrCell(row=1, col=1, text=RESOLVABLE_ENTITY_TITLE, confidence=HIGH_CONF),
            OcrCell(row=1, col=2, text="FY2024", confidence=HIGH_CONF),
            OcrCell(row=2, col=1, text="REVENUE", confidence=HIGH_CONF),
            OcrCell(row=2, col=2, text="2680", confidence=HIGH_CONF),
            OcrCell(row=3, col=1, text="PROFIT", confidence=HIGH_CONF),
            OcrCell(row=3, col=2, text="2210", confidence=LOW_CONF),
        ]
        return OcrPageResult(
            page_no=page_no,
            tables=[OcrTable(n_rows=3, n_cols=2, cells=cells)],
        )


@pytest.fixture
def store(tmp_sqlite_factory):
    fs = FactStore(tmp_sqlite_factory("facts"))
    yield fs
    fs.close()


@pytest.fixture
def queue(tmp_sqlite_factory):
    q = ReviewQueue(tmp_sqlite_factory("queue"))
    yield q
    q.close()


@pytest.fixture
def registry(tmp_sqlite_factory):
    reg = MappingRegistry(tmp_sqlite_factory("registry"))
    yield reg
    reg.close()


def _init_three(store, registry, queue) -> None:
    store.init_schema()
    queue.init_schema()
    registry.init_schema()


# ===========================================================================
# W3a — 扫描型 PDF 经家族 OCR 抽取并真正入库（不再只 enqueue）
# ===========================================================================
def test_scanned_pdf_ocr_produces_facts(store, registry, queue, scanned_pdf_path):
    """W3a：作为离线运营者，我把扫描型 PDF（无文本层）交给 ingest_file 并注入家族 OCR 后端，
    它应【真正 OCR 抽表入库】，而不是只把文件丢进复核队列。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    assert store.count() == 0
    report = ingest_file(
        scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend()
    )
    assert report.status == "ok"
    assert report.error is None
    assert report.n_facts_ingested >= 1
    assert store.count() >= 1


def test_scanned_pdf_ocr_fact_retrievable(store, registry, queue, scanned_pdf_path):
    """W3a：扫描件入库后我能按 REVENUE / ACME_HK / FY2024 精确查回该值（2680）——
    证明扫描 PDF 的内容真正进入检索（user story 2），而非静默躺在复核队列。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend())
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024", channel="TOTAL")
    assert len(rows) == 1
    assert rows[0].value == 2680.0
    assert rows[0].review_status in VISIBLE_REVIEW_STATUSES


def test_scanned_pdf_ocr_low_confidence_routed_to_review(
    store, registry, queue, scanned_pdf_path
):
    """W3a：保留既有纪律——低置信 OCR 格（confidence < min_confidence）仍路由复核队列，
    reason='low_confidence_ocr'，payload 含 locator/text/confidence，绝不静默混入。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend())
    low_conf = [
        it for it in queue.list_pending() if it.reason == LOW_CONFIDENCE_REASON
    ]
    assert low_conf, "低置信 OCR 格未路由进复核队列"
    item = low_conf[0]
    assert "R3C2" in item.payload["locator"]
    assert item.payload["confidence"] == LOW_CONF


def test_scanned_pdf_ocr_provenance_preserved(store, registry, queue, scanned_pdf_path):
    """W3a：OCR 入库事实带完整血缘——source_doc_id=文件名、source_file_hash 非空、
    source_locator 含 page{N}_table{M}!R{r}C{c}、extractor_version 随注入后端 version。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend())
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert len(rows) == 1
    fact = rows[0]
    assert fact.source_doc_id == scanned_pdf_path.name
    assert fact.source_file_hash
    assert "page1_table1" in fact.source_locator
    assert "R2C2" in fact.source_locator
    assert fact.extractor_version == "fake_ocr@1"  # 血缘随后端 version（GridExtractor 同范式）


def test_scanned_pdf_ocr_backend_called_per_page(store, registry, queue, scanned_pdf_path):
    """W3a：3 页扫描 PDF -> OCR 后端被逐页调用（页号 1-based 覆盖全部页），
    证明全文件被渲染识别，而非整文件一刀切丢复核。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    fake = _FakeOcrBackend()
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=fake)
    assert sorted(fake.calls) == [1, 2, 3]


def test_default_ocr_backend_is_family_pdfspine(
    store, registry, queue, scanned_pdf_path, monkeypatch
):
    """W3a：未注入 ocr_backend 时，扫描分支默认走【家族 OCR】PdfSpineOcrBackend
    （pdfspine 的离线确定性 PP-OCRv5），而非 GPU-gated PaddleOCR-VL——离线即用。
    用 spy 捕获传入 extract_grids 的后端类型，短路返回 [] 以免真跑 OCR。"""
    from ragspine.ingestion.structured.ingestion import ingest_file
    import ragspine.ingestion.structured.ingestion as ingestion_mod
    from ragspine.extraction.extractors.pdf_scanned_extractor import PdfSpineOcrBackend

    _init_three(store, registry, queue)
    captured: dict[str, object] = {}
    real = ingestion_mod.pdf_scanned_extractor.extract_grids

    def _spy(path, backend, *, min_confidence=0.85, queue=None):  # noqa: ARG001
        captured["backend"] = backend
        return []

    monkeypatch.setattr(ingestion_mod.pdf_scanned_extractor, "extract_grids", _spy)
    ingest_file(scanned_pdf_path, store, registry, queue)  # 不注入 -> 默认家族 OCR
    assert isinstance(captured["backend"], PdfSpineOcrBackend)
    assert real is not _spy  # sanity：确有真实实现被替换


def test_scanned_pdf_ocr_dry_run_writes_nothing(
    store, registry, queue, scanned_pdf_path
):
    """W3a：扫描型 PDF 的 dry_run 仍是「只看不动」——store 与 queue 都零写入，
    n_facts_ingested == 0 且 n_enqueued_review == 0（含低置信入队也不发生）。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    report = ingest_file(
        scanned_pdf_path, store, registry, queue,
        dry_run=True, ocr_backend=_FakeOcrBackend(),
    )
    assert report.dry_run is True
    assert report.n_facts_ingested == 0
    assert report.n_enqueued_review == 0
    assert store.count() == 0
    assert queue.list_pending() == []


def test_scanned_pdf_ocr_idempotent(store, registry, queue, scanned_pdf_path):
    """W3a：同一扫描 PDF 重复 ingest 幂等——靠 fact_metric 唯一键 upsert，库内事实不翻倍。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend())
    first = store.count()
    assert first >= 1
    ingest_file(scanned_pdf_path, store, registry, queue, ocr_backend=_FakeOcrBackend())
    assert store.count() == first


def test_scanned_pdf_empty_ocr_not_silently_dropped(
    store, registry, queue, scanned_pdf_path
):
    """W3a：若 OCR 未识别出任何表格，扫描件绝不被静默丢弃——必须留下复核项或告警，
    让运营看到「这份文件被挡下、原因是什么」。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    report = ingest_file(
        scanned_pdf_path, store, registry, queue,
        ocr_backend=_FakeOcrBackend(empty=True),
    )
    assert report.n_facts_ingested == 0
    assert store.count() == 0
    assert report.n_enqueued_review > 0 or len(report.warnings) > 0
