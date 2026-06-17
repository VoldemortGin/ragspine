"""生成二期（PDF 线）合成 fixture + ground truth（确定性、硬编码）。

覆盖 PRD 二期全部分诊形态与下游校验向量（user stories 8、9、11、15）：
    - digital.pdf    数字型：第 1 页一张规整带边框表格（给 Docling 解析）+ 标题；
                     第 2 页一段叙述文本。
    - scanned.pdf    扫描型：整页位图、无文本层。
    - ocr_scan.pdf   OCR 扫描型：整页位图 + 隐形文本层（text render mode 3）。
    - mixed.pdf      混合型：2 页数字 + 2 页扫描。
    - ppt_export.pdf PowerPoint 导出：1 页数字内容 + producer 元数据标记。

产出：
    data/fixtures/pdf/{digital,scanned,ocr_scan,mixed,ppt_export}.pdf
    data/fixtures/pdf/pdf_ground_truth.json
        —— 每文件期望 verdict / 逐页 kind / ask_for_pptx；digital.pdf 逐格表格真值
           （行列名 + 数值）；dual-channel 测试向量（两组 ChannelFact 输入与期望的
           agreed / conflict / only 划分）。

绘制用 reportlab（BSD-3，dev 依赖），读回自校验用 pypdfium2
（页数 / 文本量 / 图片覆盖 / metadata 符合设计）。
从项目根目录运行：.venv/bin/python scripts/make_fixtures_pdf.py
"""

import io
import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen.canvas import Canvas

OUT_DIR = ROOT_DIR / "data" / "fixtures" / "pdf"
GT_PATH = OUT_DIR / "pdf_ground_truth.json"

DIGITAL_PATH = OUT_DIR / "digital.pdf"
SCANNED_PATH = OUT_DIR / "scanned.pdf"
OCR_SCAN_PATH = OUT_DIR / "ocr_scan.pdf"
MIXED_PATH = OUT_DIR / "mixed.pdf"
PPT_EXPORT_PATH = OUT_DIR / "ppt_export.pdf"

# 页面尺寸（A4，沿用旧 fixture 的整数点尺寸，保证分诊度量可复现）。
PAGE_W, PAGE_H = 595, 842

# 渲染分辨率（位图页用），确定性。
SCAN_DPI = 150

# PowerPoint 导出 producer（pdf_router.route 的 ask_for_pptx 检测依据）。
PPT_PRODUCER = "Microsoft® PowerPoint® for Microsoft 365"

# --- digital.pdf 第 1 页表格真值（硬编码，给 Docling 解析）-------------------
TABLE_TITLE = "ACME Hong Kong Financial Performance"
TABLE_ROW_HEADERS = ["REVENUE", "NEWSALES", "PROFIT"]
TABLE_COL_HEADERS = ["FY2022", "FY2023", "FY2024"]
# 行 -> 各列数值（与列表头一一对应）。
TABLE_VALUES = {
    "REVENUE": [2100, 2350, 2680],
    "NEWSALES": [3800, 4200, 4750],
    "PROFIT": [1900, 2050, 2210],
}

# 第 2 页叙述文本（数字型、低图片覆盖）。
NARRATIVE_TEXT = (
    "ACME Hong Kong delivered resilient performance across fiscal years 2022 "
    "through 2024. Value of New Business expanded steadily, supported by an "
    "enlarged agency force and deeper bancassurance partnerships. Annualised "
    "New Premium and Operating Profit After Tax both maintained an upward "
    "trajectory, reflecting disciplined execution of the multi-channel growth "
    "strategy and continued investment in customer experience and digital "
    "distribution capabilities throughout the reporting period."
)


# ---------------------------------------------------------------------------
# 绘制原语（reportlab 原点在左下角；沿用旧版「顶部起算」坐标语义，统一换算）
# ---------------------------------------------------------------------------

def _y(y_top: float) -> float:
    """顶部起算的 y 坐标 -> reportlab 左下角原点坐标。"""
    return PAGE_H - y_top


def _new_canvas(path) -> Canvas:
    """新建固定页面尺寸的 Canvas（invariant=1：固定日期 / ID，产物可复现）。"""
    return Canvas(str(path), pagesize=(PAGE_W, PAGE_H), invariant=1)


def _draw_clean_table(c: Canvas, origin_x: float, origin_y: float) -> None:
    """画一张干净的带边框表格（4 行 × 4 列：左上角 + 3 期间列；3 指标行）。

    标准字体、宽松间距、完整表格线 —— 务必规整，供 Docling 解析。
    origin_y 为表格上沿（顶部起算），与旧版坐标语义一致。
    """
    n_rows = 1 + len(TABLE_ROW_HEADERS)   # 表头行 + 指标行
    n_cols = 1 + len(TABLE_COL_HEADERS)   # 行标签列 + 期间列
    col_w = 110.0
    row_h = 34.0

    # 网格线（横 + 纵），完整闭合边框。
    c.setLineWidth(1.0)
    c.setStrokeColorRGB(0, 0, 0)
    for i in range(n_rows + 1):
        y = _y(origin_y + i * row_h)
        c.line(origin_x, y, origin_x + n_cols * col_w, y)
    for j in range(n_cols + 1):
        x = origin_x + j * col_w
        c.line(x, _y(origin_y), x, _y(origin_y + n_rows * row_h))

    c.setFont("Helvetica", 12)
    c.setFillColorRGB(0, 0, 0)

    def _cell_text(r: int, col: int, text: str) -> None:
        # 文本基线置于格内偏左、纵向居中位置（宽松间距）。
        x = origin_x + col * col_w + 10
        y = _y(origin_y + r * row_h + row_h / 2 + 4)
        c.drawString(x, y, text)

    # 表头行：左上角空 + 期间列
    for col, header in enumerate(TABLE_COL_HEADERS, start=1):
        _cell_text(0, col, header)
    # 指标行
    for r, metric in enumerate(TABLE_ROW_HEADERS, start=1):
        _cell_text(r, 0, metric)
        for col, _header in enumerate(TABLE_COL_HEADERS, start=1):
            val = TABLE_VALUES[metric][col - 1]
            _cell_text(r, col, str(val))


def _draw_digital_table_page(c: Canvas) -> None:
    """digital.pdf 第 1 页：标题 + 干净表格（数字型、低图片覆盖）。"""
    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(60, _y(70), TABLE_TITLE)
    _draw_clean_table(c, origin_x=60, origin_y=110)
    c.showPage()


def _draw_digital_text_page(c: Canvas) -> None:
    """digital.pdf 第 2 页：一段叙述文本（数字型、低图片覆盖）。"""
    c.setFont("Helvetica", 12)
    c.setFillColorRGB(0, 0, 0)
    lines = simpleSplit(NARRATIVE_TEXT, "Helvetica", 12, 475)
    y = _y(70 + 12)  # 首行基线（文本框上沿 70，向下一个字号）
    for line in lines:
        c.drawString(60, y, line)
        y -= 14.4  # 1.2 倍行距
    c.showPage()


def _draw_scanned_page(c: Canvas, png_bytes: bytes,
                       hidden_text: str | None = None) -> None:
    """画一页：整页位图铺满页面；可选隐形文本层（text render mode 3）。

    hidden_text 非空时模拟「OCR 过的扫描件」：文本层存在但视觉不可见
    （render mode 3：既不填充也不描边，但可被文本抽取读出）。
    """
    c.drawImage(ImageReader(io.BytesIO(png_bytes)), 0, 0,
                width=PAGE_W, height=PAGE_H)
    if hidden_text:
        t = c.beginText(60, _y(80))
        t.setFont("Helvetica", 12)
        t.setTextRenderMode(3)
        t.textLine(hidden_text)
        c.drawText(t)
    c.showPage()


def _render_page_to_png(path, page_no: int) -> bytes:
    """用 pypdfium2 把已有 PDF 的某页（0-based）渲染为 PNG 字节（合成扫描底图用）。"""
    doc = pdfium.PdfDocument(str(path))
    try:
        page = doc[page_no]
        try:
            bitmap = page.render(scale=SCAN_DPI / 72)
            try:
                pil_image = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        doc.close()
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 各 fixture 构建
# ---------------------------------------------------------------------------

def _make_digital() -> None:
    c = _new_canvas(DIGITAL_PATH)
    _draw_digital_table_page(c)
    _draw_digital_text_page(c)
    c.save()


def _digital_source_png() -> bytes:
    """渲染 digital.pdf 第 1 页为 PNG，作为扫描位图的底图来源。"""
    return _render_page_to_png(DIGITAL_PATH, 0)


def _make_scanned(png_bytes: bytes) -> None:
    c = _new_canvas(SCANNED_PATH)
    for _ in range(3):
        _draw_scanned_page(c, png_bytes)  # 无文本层
    c.save()


def _make_ocr_scan(png_bytes: bytes) -> None:
    c = _new_canvas(OCR_SCAN_PATH)
    hidden = (
        "ACME Hong Kong Financial Performance REVENUE NEWSALES PROFIT "
        "FY2022 FY2023 FY2024 2100 2350 2680"
    )
    for _ in range(2):
        _draw_scanned_page(c, png_bytes, hidden_text=hidden)
    c.save()


def _make_mixed(png_bytes: bytes) -> None:
    c = _new_canvas(MIXED_PATH)
    # 2 页数字
    _draw_digital_table_page(c)
    _draw_digital_text_page(c)
    # 2 页扫描
    _draw_scanned_page(c, png_bytes)
    _draw_scanned_page(c, png_bytes)
    c.save()


def _make_ppt_export() -> None:
    c = _new_canvas(PPT_EXPORT_PATH)
    c.setProducer(PPT_PRODUCER)
    c.setCreator(PPT_PRODUCER)
    _draw_digital_table_page(c)  # 1 页数字内容
    c.save()


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

def _digital_table_truth() -> dict:
    """digital.pdf 第 1 页表格逐格真值（'R{行}C{列}' 1-based，含行列名 + 数值）。

    表格布局（R1 为表头行、C1 为行标签列）：
        R1C2..R1C4 = 期间表头；R2C1..R4C1 = 指标名；R2C2.. = 数值。
    sheet 命名 'page1_table1'，与抽取器 source_locator 约定一致。
    """
    cells: dict[str, object] = {}
    # 表头行（R1）：C1 空（左上角），C2..C4 期间
    for c, col in enumerate(TABLE_COL_HEADERS, start=2):
        cells[f"R1C{c}"] = col
    # 指标行（R2..R4）
    for r, metric in enumerate(TABLE_ROW_HEADERS, start=2):
        cells[f"R{r}C1"] = metric
        for c, _col in enumerate(TABLE_COL_HEADERS, start=2):
            cells[f"R{r}C{c}"] = TABLE_VALUES[metric][c - 2]
    return {
        "sheet": "page1_table1",
        "title": TABLE_TITLE,
        "n_rows": 1 + len(TABLE_ROW_HEADERS),
        "n_cols": 1 + len(TABLE_COL_HEADERS),
        "row_headers": list(TABLE_ROW_HEADERS),
        "col_headers": list(TABLE_COL_HEADERS),
        "cells": cells,
    }


def _dual_channel_vectors() -> dict:
    """双通道校验测试向量：两组 ChannelFact 输入 + 期望划分。

    设计（基于 digital.pdf 表格真值）：
        通道 A（docling）   抽到 REVENUE/NEWSALES/PROFIT × FY2024。
        通道 B（text_layer）抽到 REVENUE（值一致）/NEWSALES（值冲突）/ROE（仅 B 独有）。
        => REVENUE        -> agreed
           NEWSALES         -> conflict（4750 vs 9999）
           PROFIT        -> only_in_a
           ROE         -> only_in_b
    """
    def _fact(metric, value, locator, channel_name):
        return {
            "metric_code": metric,
            "entity": "ACME_HK",
            "period_type": "FY",
            "period": "FY2024",
            "channel": "TOTAL",
            "value": value,
            "source_locator": locator,
            "channel_name": channel_name,
        }

    facts_a = [
        _fact("REVENUE", 2680, "page1_table1!R2C4", "docling"),
        _fact("NEWSALES", 4750, "page1_table1!R3C4", "docling"),
        _fact("PROFIT", 2210, "page1_table1!R4C4", "docling"),
    ]
    facts_b = [
        _fact("REVENUE", 2680, "p1t1_text!revenue_fy2024", "text_layer"),
        _fact("NEWSALES", 9999, "p1t1_text!newsales_fy2024", "text_layer"),
        _fact("ROE", 14, "p1t1_text!roe_fy2024", "text_layer"),
    ]
    return {
        "facts_a": facts_a,
        "facts_b": facts_b,
        "tolerance": 0.0,
        "expect": {
            "agreed_keys": [["REVENUE", "ACME_HK", "FY", "FY2024", "TOTAL"]],
            "conflict_keys": [["NEWSALES", "ACME_HK", "FY", "FY2024", "TOTAL"]],
            "only_in_a_keys": [["PROFIT", "ACME_HK", "FY", "FY2024", "TOTAL"]],
            "only_in_b_keys": [["ROE", "ACME_HK", "FY", "FY2024", "TOTAL"]],
            "n_auto_passed": 1,
            "n_enqueued": 3,  # 1 conflict + 2 single-only
        },
    }


def _build_ground_truth() -> dict:
    return {
        "files": {
            "digital.pdf": {
                "verdict": "digital",
                "page_kinds": ["digital", "digital"],
                "ask_for_pptx": False,
                "table": _digital_table_truth(),
            },
            "scanned.pdf": {
                "verdict": "scanned",
                "page_kinds": ["img_scan", "img_scan", "img_scan"],
                "ask_for_pptx": False,
            },
            "ocr_scan.pdf": {
                "verdict": "ocr_scan",
                "page_kinds": ["ocr_scan", "ocr_scan"],
                "ask_for_pptx": False,
            },
            "mixed.pdf": {
                "verdict": "mixed",
                "page_kinds": ["digital", "digital", "img_scan", "img_scan"],
                "ask_for_pptx": False,
                # 混合型逐页路由计划（页号 1-based -> 管线名）。
                "channel_plan": {
                    "1": "digital_extractor",
                    "2": "digital_extractor",
                    "3": "scanned_extractor",
                    "4": "scanned_extractor",
                },
            },
            "ppt_export.pdf": {
                "verdict": "digital",
                "page_kinds": ["digital"],
                "ask_for_pptx": True,
                "producer": PPT_PRODUCER,
            },
        },
        "dual_channel": _dual_channel_vectors(),
    }


# ---------------------------------------------------------------------------
# 自校验（pypdfium2 读回，度量口径与 src/pdf_router.py 一致）
# ---------------------------------------------------------------------------

def _page_text(page) -> str:
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range()
    finally:
        textpage.close()


def _page_chars(page) -> int:
    return len(_page_text(page).strip())


def _page_cover(page) -> float:
    width, height = page.get_size()
    page_area = (width * height) or 1.0
    cover = 0.0
    for obj in page.get_objects(
        filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,), max_depth=15
    ):
        left, bottom, right, top = obj.get_bounds()
        left, right = max(left, 0.0), min(right, width)
        bottom, top = max(bottom, 0.0), min(top, height)
        if right > left and top > bottom:
            cover += (right - left) * (top - bottom) / page_area
    return min(cover, 1.0)


def self_verify(gt: dict) -> None:
    """读回各 fixture，断言页数 / 文本量 / 图片覆盖 / metadata 符合设计。"""
    # digital.pdf：2 页、均有实质文本、低图片覆盖、表格数值与标题可读出
    d = pdfium.PdfDocument(str(DIGITAL_PATH))
    assert len(d) == 2, len(d)
    assert _page_chars(d[0]) >= 50, _page_chars(d[0])
    assert _page_cover(d[0]) < 0.55, _page_cover(d[0])
    assert _page_chars(d[1]) >= 50, _page_chars(d[1])
    txt0 = _page_text(d[0])
    assert TABLE_TITLE in txt0, "标题缺失"
    for metric in TABLE_ROW_HEADERS:
        assert metric in txt0, f"行名 {metric} 缺失"
    for col in TABLE_COL_HEADERS:
        assert col in txt0, f"列名 {col} 缺失"
    assert "2680" in txt0 and "4750" in txt0, "数值缺失"
    d.close()

    # scanned.pdf：3 页、无文本层、高图片覆盖
    s = pdfium.PdfDocument(str(SCANNED_PATH))
    assert len(s) == 3, len(s)
    for p in s:
        assert _page_chars(p) < 50, _page_chars(p)
        assert _page_cover(p) >= 0.55, _page_cover(p)
    s.close()

    # ocr_scan.pdf：2 页、有文本层（隐形）、高图片覆盖
    o = pdfium.PdfDocument(str(OCR_SCAN_PATH))
    assert len(o) == 2, len(o)
    for p in o:
        assert _page_chars(p) >= 50, _page_chars(p)
        assert _page_cover(p) >= 0.55, _page_cover(p)
    o.close()

    # mixed.pdf：4 页 = 2 数字（低覆盖、有文本）+ 2 扫描（高覆盖、无文本）
    m = pdfium.PdfDocument(str(MIXED_PATH))
    assert len(m) == 4, len(m)
    assert _page_chars(m[0]) >= 50 and _page_cover(m[0]) < 0.55
    assert _page_chars(m[1]) >= 50 and _page_cover(m[1]) < 0.55
    assert _page_chars(m[2]) < 50 and _page_cover(m[2]) >= 0.55
    assert _page_chars(m[3]) < 50 and _page_cover(m[3]) >= 0.55
    m.close()

    # ppt_export.pdf：1 页数字 + producer 元数据命中 PowerPoint
    pe = pdfium.PdfDocument(str(PPT_EXPORT_PATH))
    assert len(pe) == 1, len(pe)
    assert _page_chars(pe[0]) >= 50
    producer = pe.get_metadata_dict().get("Producer", "")
    assert "PowerPoint" in producer, producer
    pe.close()

    # ground truth 自洽性
    assert set(gt["files"]) == {
        "digital.pdf", "scanned.pdf", "ocr_scan.pdf", "mixed.pdf", "ppt_export.pdf"
    }
    dc = gt["dual_channel"]["expect"]
    assert dc["n_auto_passed"] == 1 and dc["n_enqueued"] == 3

    print("self-verify: 5 个 PDF fixture 页数/文本/覆盖/metadata + ground truth 全部通过")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _make_digital()
    png = _digital_source_png()
    _make_scanned(png)
    _make_ocr_scan(png)
    _make_mixed(png)
    _make_ppt_export()

    gt = _build_ground_truth()
    GT_PATH.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")

    self_verify(gt)

    print(f"fixture 已生成于 {OUT_DIR}")
    for p in (DIGITAL_PATH, SCANNED_PATH, OCR_SCAN_PATH, MIXED_PATH, PPT_EXPORT_PATH):
        print(f"  {p.name}")
    print(f"  ground_truth: {GT_PATH.name}")


if __name__ == "__main__":
    main()
