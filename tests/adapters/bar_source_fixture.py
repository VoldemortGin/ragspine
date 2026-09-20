"""Self-authored PDF/font chart, independent of the ignored AIA corpus."""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

import pdfspine
from tests.adapters.test_source_paint import _FontInsertionPage

from enterprise_pdf_rag.adapters.chart_semantic_schemas import FigureDescriptionDTO
from enterprise_pdf_rag.adapters.description_mapping import map_description
from enterprise_pdf_rag.adapters.figure_reasoning import PreparedFigure, prepare_figure
from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.chart_qa.displayed_evidence import ACTUAL_FX_CONTEXT
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    Evidence,
    ExecutionMode,
    NumericObservation,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.models import PageInput


@dataclass(frozen=True)
class BarSource:
    pdf: bytes
    page: PageInput
    prepared: PreparedFigure
    chart: ChartIR
    previous_description: TextDescription
    raw_description: bytes


def bar_source(*, hidden_footer: bool = False, cover_footer: bool = False) -> BarSource:
    font = (
        Path(__file__)
        .parents[1]
        .joinpath("fixtures/authored-donut-ascii.ttf")
        .read_bytes()
    )
    with pdfspine.open() as document:
        page = document.new_page(width=300, height=220)
        cast(_FontInsertionPage, page).insert_font(fontname="Authored", fontbuffer=font)
        for left, top in ((45.0, 65.0), (125.0, 80.0), (205.0, 95.0)):
            page.draw_rect(
                (left, top, left + 24.0, 145.0), color=None, fill=(0.2, 0.4, 0.6)
            )
        for text, origin, size in (
            ("Expense Ratio", (110.0, 40.0), 9.0),
            ("15%", (49.0, 61.0), 8.0),
            ("6%", (212.0, 91.0), 8.0),
            ("1H21", (47.0, 155.0), 8.0),
            ("1H22", (127.0, 155.0), 8.0),
            ("1H23", (207.0, 155.0), 8.0),
        ):
            page.insert_text(origin, text, fontname="Authored", fontsize=size)
        page.insert_text(
            (10.0, 205.0), ACTUAL_FX_CONTEXT, fontname="Authored", fontsize=3.0
        )
        if hidden_footer:
            stream = page.get_contents()[-1]
            document.update_stream(
                stream, document.xref_stream(stream).replace(b"BT", b"BT\n3 Tr")
            )
        if cover_footer:
            page.draw_rect((9.0, 199.0, 200.0, 209.0), color=None, fill=(1.0, 1.0, 1.0))
        pdf = document.tobytes()
    extracted = PdfspineDocumentAdapter().extract_document(pdf).pages[0]
    native = extracted.native_svg.encode()
    source = sha256(pdf).hexdigest()
    page_input = PageInput(
        "a" * 64,
        source,
        0,
        300.0,
        220.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        TextSidecar("source-text-v1", source, 0, extracted.text_spans),
    )
    prepared = prepare_figure(
        page=page_input,
        native_svg=native,
        bbox=(20.0, 20.0, 280.0, 175.0),
        region_id="authored-bar",
    )

    def evidence(text: str) -> Evidence:
        element = next(
            element for element in prepared.svg.elements if element.text == text
        )
        return Evidence(
            (element.element_id,),
            Verification.PENDING,
            Confidence(None, "fixture independent branch"),
        )

    def field(text: str) -> TextField:
        return TextField(text, evidence(text))

    points = tuple(
        ChartPoint(
            f"p-{period}",
            field("Expense Ratio"),
            field(period),
            field("%"),
            NumericObservation(
                Decimal(value) if value else None,
                ValueKind.EXPLICIT if value else ValueKind.UNAVAILABLE,
                evidence(value if value else period),
            ),
        )
        for period, value in (("1H21", "15"), ("1H22", None), ("1H23", "6"))
    )
    # Unit evidence must come from each value's own full source occurrence.
    last_unit = next(
        element
        for element in prepared.svg.elements
        if element.text == "%"
        and element.source_span_id
        == next(e for e in prepared.svg.elements if e.text == "6%").source_span_id
    )
    from dataclasses import replace

    points = (
        *points[:-1],
        replace(
            points[-1],
            unit=TextField(
                "%",
                Evidence(
                    (last_unit.element_id,),
                    Verification.PENDING,
                    Confidence(None, "independent fixture"),
                ),
            ),
        ),
    )
    chart = ChartIR(
        prepared.svg.binding,
        "bar",
        (),
        points,
        "independent-fixture-chart",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        title=field("Expense Ratio"),
    )
    claims = []
    for index, point in enumerate(points):
        ids = (
            (
                *point.series.evidence.element_ids,
                *point.category.evidence.element_ids,
                *point.value.evidence.element_ids,
                *point.unit.evidence.element_ids,
            )
            if point.value.value is not None
            else (
                *point.series.evidence.element_ids,
                *point.category.evidence.element_ids,
            )
        )
        if index == 0:
            ids = (ids[0], *ids)
        claims.append(
            {
                "text": f"Expense Ratio for {point.category.text}: {point.value.value} %."
                if point.value.value is not None
                else f"Expense Ratio for {point.category.text}: [value unavailable].",
                "series": "Expense Ratio",
                "category": point.category.text,
                "unit": "%" if point.value.value is not None else None,
                "value": str(point.value.value)
                if point.value.value is not None
                else None,
                "period": None,
                "evidence": {"element_ids": ids, "confidence": "high"},
            }
        )
    raw = json.dumps(
        {
            "schema_version": "figure-description-v1",
            "svg_digest": prepared.svg.digest,
            "claims": claims,
            "diagnostics": [],
        }
    ).encode()
    previous = map_description(
        prepared.svg,
        FigureDescriptionDTO.model_validate_json(raw),
        producer="independent-fixture-description",
    ).description
    assert previous is not None
    return BarSource(pdf, page_input, prepared, chart, previous, raw)
