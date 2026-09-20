"""Real PDF boundary contracts; no substitute PDF parser or network."""

from xml.etree import ElementTree

import pdfspine

from enterprise_pdf_rag.adapters.pdfspine_figure import PdfspineFigureParser
from enterprise_pdf_rag.figures.models import Verification


def labelled_chart_pdf() -> bytes:
    document = pdfspine.open()
    page = document.new_page(width=360, height=240)
    page.insert_text((35, 35), "Revenue (USDm)", fontsize=14)
    page.draw_rect((55, 105, 105, 190), fill=(0.1, 0.4, 0.8), color=(0.1, 0.4, 0.8))
    page.draw_rect((165, 65, 215, 190), fill=(0.1, 0.4, 0.8), color=(0.1, 0.4, 0.8))
    page.insert_text((50, 210), "2024: 10", fontsize=12)
    page.insert_text((160, 210), "2025: 15", fontsize=12)
    result: bytes = document.tobytes()
    document.close()
    return result


def test_labelled_pdf_region_has_readable_svg_and_source_anchors() -> None:
    source = labelled_chart_pdf()
    result = PdfspineFigureParser().extract(source, page_index=0, bbox=(20.0, 15.0, 250.0, 225.0))
    svg = ElementTree.fromstring(result.svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    labels = {element.text for element in svg.findall(".//s:text", ns)}
    assert {"Revenue (USDm)", "2024: 10", "2025: 15"} <= labels
    assert len(svg.findall(".//s:rect", ns)) >= 2
    assert svg.findall(".//s:image", ns) == []
    assert result.source.page_index == 0
    assert result.source.bbox == (20.0, 15.0, 250.0, 225.0)
    assert result.verification is Verification.PENDING
    assert {element.anchor.bbox for element in result.elements if not element.text} == {
        (55.0, 105.0, 105.0, 190.0),
        (165.0, 65.0, 215.0, 190.0),
    }
    assert all(
        element.anchor.document_sha256 == result.source.document_sha256
        for element in result.elements
    )
    assert all(element.anchor.page_index == 0 for element in result.elements)
    assert {element.element_id for element in result.elements} <= {
        element.attrib["id"] for element in svg.iter() if "id" in element.attrib
    }
