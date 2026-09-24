"""用 pdfspine 把 PDF 页渲染成 PNG（入库期，确定性）。

默认参数的取舍：
- ``DEFAULT_PAGE_IMAGE_DPI = 144``：2 倍于 PDF 的 72pt/in，8–9pt 的图注、坐标轴刻度和表格小字仍清晰可读。
- ``DEFAULT_PAGE_IMAGE_MAX_SIDE = 1568``：Claude 读图时长边超过约 1568px 的图会在服务端先缩小，
  再大只是白白增加体积和 token。16:9 幻灯片（960×540pt）按 144dpi 是 1920px，封顶后为 1568×882
  （约 117dpi），实测单页 PNG 约 0.2MB；A4 竖版封顶后约 134dpi。
缩放系数取 ``min(dpi / 72, max_side / 页面长边pt)``，两个约束都满足。
"""

from collections.abc import Iterable
from pathlib import Path

from ragspine.retrieval.page_images.store import RenderedPage

DEFAULT_PAGE_IMAGE_DPI = 144
DEFAULT_PAGE_IMAGE_MAX_SIDE = 1568


def pdf_page_count(path: str | Path) -> int:
    """PDF 页数；打不开 / 不是 PDF 时抛 pdfspine 的异常（由调用方归一）。"""
    import pdfspine  # 惰性 import：只在真的关联了 PDF 时才需要

    doc = pdfspine.open(str(path))
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def render_pdf_pages(
    path: str | Path,
    *,
    dpi: int = DEFAULT_PAGE_IMAGE_DPI,
    max_side: int = DEFAULT_PAGE_IMAGE_MAX_SIDE,
    pages: Iterable[int] | None = None,
) -> list[RenderedPage]:
    """渲染指定物理页（1 起；默认全部），按页码升序返回。"""
    if dpi <= 0 or max_side <= 0:
        raise ValueError(f"dpi / max_side 必须为正数：dpi={dpi} max_side={max_side}")
    import pdfspine

    doc = pdfspine.open(str(path))
    try:
        wanted = sorted(set(pages)) if pages is not None else range(1, doc.page_count + 1)
        out: list[RenderedPage] = []
        for number in wanted:
            page = doc[number - 1]
            rect = page.rect
            scale = min(dpi / 72, max_side / max(rect.width, rect.height))
            pix = page.get_pixmap(matrix=pdfspine.Matrix(scale, scale))
            out.append(
                RenderedPage(
                    page=number, png=pix.tobytes("png"), width=pix.width, height=pix.height
                )
            )
        return out
    finally:
        doc.close()
