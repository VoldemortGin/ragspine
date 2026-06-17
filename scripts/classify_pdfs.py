"""PDF 文档普查：按"数字型(有文本层) / 扫描型 / OCR扫描型 / 混合"分类。

用法:
    python3 scripts/classify_pdfs.py <PDF文件或目录> [更多路径...]
    python3 scripts/classify_pdfs.py ~/Documents/kb_docs --csv report.csv

原理（逐页两个信号）:
    1. 可提取文本字符数  —— 有文本层的页能直接抽出文字
    2. 图片对页面的覆盖率 —— 扫描页本质是一张铺满页面的大图
    组合判定:
        文本多 + 覆盖低  -> digital   数字型页（PPT导出/Word导出等，可程序化解析）
        文本多 + 覆盖高  -> ocr_scan  扫描底图+文本层（多半被OCR过，文本质量存疑）
        文本少 + 覆盖高  -> img_scan  纯扫描/图片页（需OCR）
        文本少 + 覆盖低  -> low_text  封面/纯矢量图表页（图表数据需特殊处理）
依赖: pip install pypdfium2
"""

import csv
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

TEXT_MIN_CHARS = 50   # 每页判定"有实质文本"的最少字符数（中文也适用，按字符计）
IMG_COVER_SCAN = 0.55  # 图片覆盖率超过此值视为"扫描底图"


def classify_page(page) -> dict:
    textpage = page.get_textpage()
    try:
        chars = len(textpage.get_text_range().strip())
    finally:
        textpage.close()

    width, height = page.get_size()
    page_area = (width * height) or 1.0

    cover = 0.0
    vector_paths = 0  # 矢量绘图数量：原生图表/表格线的线索
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
        kind = "ocr_scan" if cover >= IMG_COVER_SCAN else "digital"
    else:
        kind = "img_scan" if cover >= IMG_COVER_SCAN else "low_text"
    return {"kind": kind, "chars": chars, "cover": round(cover, 2), "vector_paths": vector_paths}


def classify_pdf(path: Path) -> dict:
    doc = pdfium.PdfDocument(str(path))
    pages = []
    for idx in range(len(doc)):
        page = doc[idx]
        try:
            pages.append(classify_page(page))
        finally:
            page.close()
    n = len(pages) or 1
    counts = {k: sum(1 for p in pages if p["kind"] == k) for k in ("digital", "ocr_scan", "img_scan", "low_text")}

    if counts["digital"] >= 0.9 * n:
        verdict = "数字型PDF(可程序化解析)"
    elif counts["img_scan"] + counts["ocr_scan"] >= 0.9 * n:
        verdict = "扫描型PDF(需OCR管线)" if counts["img_scan"] >= counts["ocr_scan"] else "OCR过的扫描件(文本层质量存疑)"
    else:
        verdict = "混合型(需逐页分流)"

    meta = doc.get_metadata_dict() or {}
    origin = f"{meta.get('Creator', '')} {meta.get('Producer', '')}".strip()
    # 项目特别关注点：PDF 若由 PowerPoint/Keynote 导出，应回头去要原生 pptx
    ask_for_source = any(s in origin for s in ("PowerPoint", "Keynote", "Impress"))

    chart_pages = sum(1 for p in pages if p["vector_paths"] > 50 and p["kind"] in ("digital", "low_text"))
    doc.close()

    return {
        "file": str(path),
        "pages": n,
        "verdict": verdict,
        **{k: v for k, v in counts.items()},
        "chart_like_pages": chart_pages,  # 矢量绘图密集页：原生图表/复杂表格线，值得单独处理
        "origin": origin,
        "ask_for_pptx": "是" if ask_for_source else "",
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    csv_out = None
    if "--csv" in sys.argv:
        csv_out = sys.argv[sys.argv.index("--csv") + 1]
        args = [a for a in args if a != csv_out]
    if not args:
        sys.exit(__doc__)

    pdfs: list[Path] = []
    for a in args:
        p = Path(a).expanduser()
        pdfs += sorted(p.rglob("*.pdf")) if p.is_dir() else [p]

    rows = []
    for pdf in pdfs:
        try:
            rows.append(classify_pdf(pdf))
        except Exception as e:  # 加密/损坏文件不中断普查
            rows.append({"file": str(pdf), "pages": 0, "verdict": f"读取失败: {e}"})

    for r in rows:
        flag = "  ←建议去要原生pptx" if r.get("ask_for_pptx") else ""
        print(f"[{r['verdict']}] {r['file']}  ({r['pages']}页, "
              f"digital={r.get('digital', '-')}, ocr_scan={r.get('ocr_scan', '-')}, "
              f"img_scan={r.get('img_scan', '-')}, low_text={r.get('low_text', '-')}, "
              f"图表密集页={r.get('chart_like_pages', '-')}){flag}")

    if csv_out and rows:
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV 已写入: {csv_out}")


if __name__ == "__main__":
    main()
