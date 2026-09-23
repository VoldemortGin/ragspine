"""Closed synthetic fixture with independently known labels and page geometry.

Only this internally generated source can receive this demo qualification.
External PDFs have no public promotion switch and remain pending.
"""

import hashlib
from dataclasses import replace
from xml.etree import ElementTree

from enterprise_pdf_rag.adapters.pdfspine_figure import PdfspineFigureParser
from ragspine.extraction.evidence.figures.models import SvgArtifact, Verification


def make_demo_figure() -> tuple[bytes, SvgArtifact]:
    import pdfspine

    document = pdfspine.open()
    try:
        page = document.new_page(width=360, height=240)
        for x, y, text in (
            (35, 35, "Revenue"),
            (190, 35, "USDm"),
            (60, 210, "2024"),
            (80, 100, "10"),
            (170, 210, "2025"),
            (190, 60, "15"),
        ):
            page.insert_text((x, y), text, fontsize=12)
        page.draw_rect((55, 110, 105, 190), fill=(0.1, 0.4, 0.8), color=(0.1, 0.4, 0.8))
        page.draw_rect((165, 70, 215, 190), fill=(0.1, 0.4, 0.8), color=(0.1, 0.4, 0.8))
        pdf: bytes = document.tobytes()
    finally:
        document.close()
    artifact = PdfspineFigureParser().extract(pdf, page_index=0, bbox=(20.0, 15.0, 250.0, 225.0))
    root = ElementTree.fromstring(artifact.svg)
    namespace = "{http://www.w3.org/2000/svg}"
    labels = {element.text for element in artifact.elements if element.text}
    expected_bounds = {(55.0, 110.0, 105.0, 190.0), (165.0, 70.0, 215.0, 190.0)}
    shapes = {element.anchor.bbox for element in artifact.elements if not element.text}
    if labels != {"Revenue", "USDm", "2024", "10", "2025", "15"} or shapes != expected_bounds:
        raise ValueError("Demo source observations differ from the authored fixture")
    if len(root.findall(namespace + "rect")) != 2 or len(root.findall(namespace + "text")) != 6:
        raise ValueError("Demo SVG is missing known source primitives")
    if (
        artifact.source.document_sha256 != hashlib.sha256(pdf).hexdigest()
        or len(artifact.warnings) != 1
    ):
        raise ValueError("Demo source contains an unexpected extraction warning")
    root.set("data-verification", "verified")
    root.set("data-verification-scope", "offline-demo/authored-fixture-v1")
    # Keep an HTML-compatible default namespace when embedding the reviewed SVG.
    for node in root.iter():
        node.tag = node.tag.removeprefix(namespace)
    root.set("xmlns", "http://www.w3.org/2000/svg")
    return pdf, replace(
        artifact,
        svg=ElementTree.tostring(root, encoding="unicode"),
        verification=Verification.VERIFIED,
        warnings=(
            "offline-demo qualification for the internally authored fixture only; not production PDF coverage",
        ),
    )
