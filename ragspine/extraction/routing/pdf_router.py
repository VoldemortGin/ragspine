"""PDF 分诊路由（二期 PDF 线）—— scripts/classify_pdfs.py 分诊逻辑的库化契约。

把孤立的 PDF 普查脚本重构为可编程库 + 路由器：逐页用「可提取文本字符数」与
「图片对页面的覆盖率」两个信号分类，再汇总成整文件 verdict，并对混合型 PDF 产出
逐页路由计划（按页分流到数字型 / 扫描型抽取器，PRD user stories 8、11）。

阈值常量沿用 scripts/classify_pdfs.py（TEXT_MIN_CHARS=50、IMG_COVER_SCAN=0.55），
保证库化前后分类规则一致。

实现已完成，dataclass 字段契约保持冻结，行为契约见 tests/extraction/routing/test_pdf_router.py。
"""

from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from ragspine.extraction.extractors.xlsx_styled_extractor import compute_file_hash

# 逐页判定阈值（沿用 scripts/classify_pdfs.py，库化前后规则一致）。
TEXT_MIN_CHARS = 50      # 每页判定「有实质文本」的最少字符数（中文按字符计）。
IMG_COVER_SCAN = 0.55    # 图片覆盖率超过此值视为「扫描底图」。

# 逐页类别取值（与 scripts/classify_pdfs.py 一致）。
PAGE_DIGITAL = "digital"       # 文本多 + 覆盖低：电子导出页，可程序化解析。
PAGE_OCR_SCAN = "ocr_scan"     # 文本多 + 覆盖高：扫描底图 + 文本层（多半被 OCR 过）。
PAGE_IMG_SCAN = "img_scan"     # 文本少 + 覆盖高：纯扫描 / 图片页（需 OCR）。
PAGE_LOW_TEXT = "low_text"     # 文本少 + 覆盖低：封面 / 纯矢量图表页。

# 整文件 verdict 取值。
VERDICT_DIGITAL = "digital"        # 数字型（可程序化解析）。
VERDICT_SCANNED = "scanned"        # 扫描型（需 OCR 管线）。
VERDICT_OCR_SCAN = "ocr_scan"      # OCR 过的扫描件（文本层质量存疑）。
VERDICT_MIXED = "mixed"            # 混合型（需逐页分流）。
VERDICT_UNREADABLE = "unreadable"  # 加密 / 损坏 / 读取失败。

# 逐页路由目标管线名（channel_plan 的取值）。
PIPELINE_DIGITAL = "digital_extractor"   # 数字型页 -> Docling 数字管线。
PIPELINE_SCANNED = "scanned_extractor"   # 扫描 / OCR 页 -> 扫描 OCR 管线。

# PowerPoint / Keynote / Impress 导出检测的生产者关键字（PRD「原生优先」）。
_EXPORT_PRODUCER_KEYWORDS = ("PowerPoint", "Keynote", "Impress")


@dataclass
class PageInfo:
    """单页分诊结果。

    字段语义约定：
        page_no:        页号（1-based）。
        kind:           逐页类别 'digital' / 'ocr_scan' / 'img_scan' / 'low_text'。
        chars:          可提取文本字符数（strip 后）。
        img_cover:      图片对页面的覆盖率 [0.0, 1.0]，多图叠放封顶为 1.0。
        vector_paths:   矢量绘图数量（原生图表 / 表格线的线索）。
    """

    page_no: int
    kind: str
    chars: int
    img_cover: float
    vector_paths: int


@dataclass
class RoutingDecision:
    """一个 PDF 文件的整体分诊与逐页路由计划。

    字段语义约定：
        path:           源文件路径（字符串）。
        file_hash:      源文件内容 hash（版本与审计血缘，与 fact_metric 对齐）。
        verdict:        整文件判定 'digital' / 'scanned' / 'ocr_scan' / 'mixed' /
                        'unreadable'（加密 / 损坏 -> 'unreadable'，不抛异常）。
        pages:          逐页 PageInfo 列表（unreadable 时为空）。
        channel_plan:   页号 -> 管线名（'digital_extractor' / 'scanned_extractor'）
                        的映射；混合型按页分流（user story 8）。
        ask_for_pptx:   生产者元数据命中 PowerPoint / Keynote / Impress -> True，
                        提示优先索取原生 pptx（user story 11，PRD「原生优先」）。
        origin_meta:    生产者 / 创建者元数据拼接串（来源判定依据）。
        error:          读取失败时的错误描述；正常为 None。
    """

    path: str
    file_hash: str | None = None
    verdict: str = VERDICT_UNREADABLE
    pages: list[PageInfo] = field(default_factory=list)
    channel_plan: dict[int, str] = field(default_factory=dict)
    ask_for_pptx: bool = False
    origin_meta: str = ""
    error: str | None = None


def classify_page(page, page_no: int = 1) -> PageInfo:
    """对单页（pypdfium2 PdfPage）做逐页分类，返回 PageInfo。

    信号与判定规则沿用 scripts/classify_pdfs.py：
        文本多(>=TEXT_MIN_CHARS) + 覆盖低 -> digital
        文本多               + 覆盖高(>=IMG_COVER_SCAN) -> ocr_scan
        文本少               + 覆盖高 -> img_scan
        文本少               + 覆盖低 -> low_text
    page_no 由调用方（route）注入实际页号（pdfium 页对象不携带自身页号）。
    """
    textpage = page.get_textpage()
    try:
        chars = len(textpage.get_text_range().strip())
    finally:
        textpage.close()

    width, height = page.get_size()
    page_area = (width * height) or 1.0

    cover = 0.0
    vector_paths = 0  # 矢量绘图数量：原生图表 / 表格线线索
    for obj in page.get_objects(max_depth=15):
        if obj.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
            # 裁剪到页面内
            left, bottom, right, top = obj.get_bounds()
            left, right = max(left, 0.0), min(right, width)
            bottom, top = max(bottom, 0.0), min(top, height)
            if right > left and top > bottom:
                cover += (right - left) * (top - bottom) / page_area
        elif obj.type == pdfium_raw.FPDF_PAGEOBJ_PATH:
            vector_paths += 1
    cover = min(cover, 1.0)  # 多图叠放时封顶

    if chars >= TEXT_MIN_CHARS:
        kind = PAGE_OCR_SCAN if cover >= IMG_COVER_SCAN else PAGE_DIGITAL
    else:
        kind = PAGE_IMG_SCAN if cover >= IMG_COVER_SCAN else PAGE_LOW_TEXT

    return PageInfo(
        page_no=page_no,
        kind=kind,
        chars=chars,
        img_cover=cover,
        vector_paths=vector_paths,
    )


def route(path) -> RoutingDecision:
    """对一个 PDF 文件分诊并生成逐页路由计划，返回 RoutingDecision。

    行为约定：
        - 逐页 classify_page 后汇总 verdict：绝大多数页同类 -> 对应单一 verdict；
          数字 / 扫描混杂 -> 'mixed' 并逐页填 channel_plan（数字页走
          'digital_extractor'，扫描 / OCR 页走 'scanned_extractor'）。
        - 生产者元数据命中 PowerPoint / Keynote / Impress -> ask_for_pptx=True。
        - file_hash 照常计算，作为版本血缘。
        - 加密 / 损坏 / 读取失败 -> verdict='unreadable'、error 记录原因、pages 为空，
          绝不抛异常（普查不能因单个坏文件中断）。
    """
    decision = RoutingDecision(path=str(path))

    # file_hash：只要文件能按字节读到就算，作为版本血缘（不存在 / 不可读则留空）。
    try:
        decision.file_hash = compute_file_hash(path)
    except OSError:
        decision.file_hash = None

    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:  # 加密 / 损坏 / 不存在 -> unreadable，绝不抛异常
        decision.error = str(e)
        return decision

    try:
        if len(doc) == 0:
            decision.error = "empty document: 0 pages"
            return decision

        pages = []
        for idx in range(len(doc)):
            page = doc[idx]
            try:
                pages.append(classify_page(page, page_no=idx + 1))
            finally:
                page.close()
        decision.pages = pages

        meta = doc.get_metadata_dict() or {}
        origin = f"{meta.get('Creator', '')} {meta.get('Producer', '')}".strip()
        decision.origin_meta = origin
        decision.ask_for_pptx = any(
            kw in origin for kw in _EXPORT_PRODUCER_KEYWORDS
        )

        decision.verdict = _aggregate_verdict(pages)
        if decision.verdict == VERDICT_MIXED:
            decision.channel_plan = {
                p.page_no: _pipeline_for_kind(p.kind) for p in pages
            }
    except Exception as e:  # 读取过程中的任何异常都降级为 unreadable
        decision.verdict = VERDICT_UNREADABLE
        decision.error = str(e)
        decision.pages = []
        decision.channel_plan = {}
        decision.ask_for_pptx = False
    finally:
        doc.close()

    return decision


def _pipeline_for_kind(kind: str) -> str:
    """逐页类别 -> 路由管线名：数字页走数字管线，其余（扫描 / OCR / 低文本）走扫描管线。"""
    return PIPELINE_DIGITAL if kind == PAGE_DIGITAL else PIPELINE_SCANNED


def _aggregate_verdict(pages: list[PageInfo]) -> str:
    """逐页 kind 汇总为整文件 verdict（阈值沿用 scripts/classify_pdfs.py 的 0.9*n）。

        绝大多数页为 digital                -> 'digital'
        绝大多数页为扫描（img_scan/ocr_scan）-> 'scanned'（img 占多）或 'ocr_scan'
        数字 / 扫描混杂                      -> 'mixed'
    """
    n = len(pages) or 1
    counts = {
        k: sum(1 for p in pages if p.kind == k)
        for k in (PAGE_DIGITAL, PAGE_OCR_SCAN, PAGE_IMG_SCAN, PAGE_LOW_TEXT)
    }

    if counts[PAGE_DIGITAL] >= 0.9 * n:
        return VERDICT_DIGITAL
    if counts[PAGE_IMG_SCAN] + counts[PAGE_OCR_SCAN] >= 0.9 * n:
        return (
            VERDICT_SCANNED
            if counts[PAGE_IMG_SCAN] >= counts[PAGE_OCR_SCAN]
            else VERDICT_OCR_SCAN
        )
    return VERDICT_MIXED
