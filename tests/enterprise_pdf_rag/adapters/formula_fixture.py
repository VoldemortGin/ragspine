"""Authored formula pages whose superscript is a real PDF `Ts`, which pdfspine cannot write."""

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from enterprise_pdf_rag.core.settings import ROOT_DIR

_FONT = ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf"
PAGE = (240.0, 160.0)


def rise_formula_pdf(path: Path, *, with_fraction: bool = True) -> Path:
    """`ROE = Net profit / Equity` with a drawn fraction rule, and `x²` written with a real `Ts`."""
    if "Authored" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Authored", str(_FONT)))
    page = canvas.Canvas(str(path), pagesize=PAGE)
    height = PAGE[1]
    if with_fraction:
        text = page.beginText(20, height - 82)
        text.setFont("Authored", 12)
        text.textOut("ROE =")
        page.drawText(text)
        text = page.beginText(62, height - 74)
        text.setFont("Authored", 11)
        text.textOut("Net profit")
        page.drawText(text)
        page.setLineWidth(0.8)
        page.line(60, height - 78, 120, height - 78)
        text = page.beginText(72, height - 94)
        text.setFont("Authored", 11)
        text.textOut("Equity")
        page.drawText(text)
    text = page.beginText(152, height - 82)
    text.setFont("Authored", 12)
    text.textOut("x")
    text.setFont("Authored", 7)
    text.setRise(5)
    text.textOut("2")
    text.setRise(0)
    page.drawText(text)
    page.showPage()
    page.save()
    return path
