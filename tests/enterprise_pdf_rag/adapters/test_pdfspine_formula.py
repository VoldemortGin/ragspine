"""The formula observation quotes the pinned source and refuses anything that drifted."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.pdfspine_document import PdfspineDocumentAdapter
from enterprise_pdf_rag.adapters.pdfspine_formula import observe_formula
from enterprise_pdf_rag.documents.models import AssetRef, Bounds, TextSidecar
from enterprise_pdf_rag.figures.models import Confidence
from enterprise_pdf_rag.processing.formula_rules import IDENTITY, rise_of
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from tests.enterprise_pdf_rag.adapters.formula_fixture import rise_formula_pdf
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    FORMULA_FRACTION_BBOX,
    FORMULA_POWER_BBOX,
    authored_pdf,
)

# The whole authored formula: the fraction on the left, the squared term on the right.
WHOLE_FORMULA_BBOX: Bounds = (
    FORMULA_FRACTION_BBOX[0],
    FORMULA_FRACTION_BBOX[1],
    FORMULA_POWER_BBOX[2],
    FORMULA_FRACTION_BBOX[3],
)
FLIPPED_RULE = ("l", ((60.0, 78.0), (120.0, 78.0)))


def _page_input(pdf: bytes) -> PageInput:
    extracted = PdfspineDocumentAdapter().extract_document(pdf).pages[0]
    svg = extracted.native_svg.encode()
    digest = sha256(pdf).hexdigest()
    return PageInput(
        "a" * 64,
        digest,
        0,
        extracted.width,
        extracted.height,
        AssetRef(sha256(svg).hexdigest(), "image/svg+xml", len(svg)),
        TextSidecar("source-text-v1", digest, 0, extracted.text_spans),
    )


def _object(
    page: PageInput,
    bbox: Bounds,
    *,
    kind: ObjectKind = ObjectKind.FORMULA,
    object_id: str = "formula-object",
) -> LayoutObject:
    span_ids = tuple(
        span.span_id
        for span in page.text.spans
        if bbox[0] <= span.bbox[0]
        and bbox[1] <= span.bbox[1]
        and span.bbox[2] <= bbox[2]
        and span.bbox[3] <= bbox[3]
    )
    return LayoutObject(
        object_id,
        kind,
        bbox,
        span_ids,
        "Model-proposed formula region",
        Confidence(None, "layout inference pending"),
    )


def _authored(tmp_path: Path, *, rule: bool = True) -> bytes:
    return authored_pdf(
        tmp_path / "formula.pdf",
        page_count=1,
        label="Formula",
        embedded_font=True,
        formula_page=True,
        formula_rule=rule,
    ).read_bytes()


def test_observe_formula_quotes_matrices_chars_and_flipped_paths(tmp_path: Path) -> None:
    pdf = _authored(tmp_path)
    page = _page_input(pdf)

    observation = observe_formula(pdf, page=page, item=_object(page, WHOLE_FORMULA_BBOX))

    assert observation.schema_version == "formula-source-observation-v1"
    assert observation.sdk.startswith("pdfspine/")
    assert (observation.page_index, observation.page_height) == (0, 160.0)
    assert tuple(run.text for run in observation.runs) == (
        "ROE =",
        "Net profit",
        "Equity",
        "x",
        "2",
    )
    for run in observation.runs:
        assert run.ctm == IDENTITY and run.direction == (1.0, 0.0)
        assert len(run.chars) == len(run.text)
        assert "".join(char.text for char in run.chars) == run.text
    # The typographic superscript is a smaller run on a raised baseline, not a real ``Ts``.
    power = observation.runs[-1]
    assert (power.size, power.origin[1], power.flags) == (7.0, 76.0, 1)
    assert rise_of(power, observation.page_height) == 0.0
    (rule,) = observation.paths
    assert (rule.path_index, rule.paint, rule.width) == (0, "s", 0.8)
    assert rule.items == (FLIPPED_RULE,)


def test_observe_formula_rise_from_reportlab_ts(tmp_path: Path) -> None:
    pdf = rise_formula_pdf(tmp_path / "rise.pdf").read_bytes()
    page = _page_input(pdf)

    observation = observe_formula(pdf, page=page, item=_object(page, WHOLE_FORMULA_BBOX))

    power = observation.runs[-1]
    assert power.text == "2"
    assert rise_of(power, observation.page_height) == 5.0
    assert observation.paths[0].items == (FLIPPED_RULE,)


def test_observe_formula_refuses_span_drift(tmp_path: Path) -> None:
    pdf = _authored(tmp_path)
    page = _page_input(pdf)
    spans = page.text.spans
    index = next(position for position, span in enumerate(spans) if span.text == "ROE =")
    drifted = replace(
        page,
        text=replace(
            page.text,
            spans=(
                *spans[:index],
                replace(spans[index], text=spans[index].text + "!"),
                *spans[index + 1 :],
            ),
        ),
    )

    with pytest.raises(ValueError, match="differs from the pinned text sidecar"):
        observe_formula(pdf, page=drifted, item=_object(drifted, WHOLE_FORMULA_BBOX))


def test_observe_formula_refuses_wrong_pdf_or_kind(tmp_path: Path) -> None:
    pdf = _authored(tmp_path)
    page = _page_input(pdf)
    item = _object(page, WHOLE_FORMULA_BBOX)

    with pytest.raises(ValueError, match="do not match the PageInput source SHA-256"):
        observe_formula(pdf + b"%", page=page, item=item)
    with pytest.raises(ValueError, match="requires a Formula layout object"):
        observe_formula(pdf, page=page, item=replace(item, kind=ObjectKind.TABLE))
    with pytest.raises(ValueError, match="references unknown source occurrences"):
        observe_formula(
            pdf, page=page, item=replace(item, source_span_ids=("span-v1-" + "0" * 64,))
        )


def test_observe_formula_ignores_paths_outside_bbox(tmp_path: Path) -> None:
    pdf = _authored(tmp_path)
    page = _page_input(pdf)

    inside = observe_formula(pdf, page=page, item=_object(page, FORMULA_FRACTION_BBOX))
    outside = observe_formula(
        pdf, page=page, item=_object(page, FORMULA_POWER_BBOX, object_id="power-object")
    )

    assert tuple(run.text for run in inside.runs) == ("ROE =", "Net profit", "Equity")
    assert inside.paths[0].items == (FLIPPED_RULE,)
    assert tuple(run.text for run in outside.runs) == ("x", "2")
    assert outside.paths == ()
