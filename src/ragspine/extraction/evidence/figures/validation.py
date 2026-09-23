"""Conservative evidence and claim checks for the explicit-label slice."""

import re
from decimal import Decimal
from xml.etree import ElementTree

from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    DescriptionClaim,
    Evidence,
    EvidenceKind,
    FailureCode,
    FigureError,
    SvgArtifact,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
)

_NUMBER = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")


def _normal(text: str) -> str:
    return " ".join(text.split())


def _numbers(text: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(match.group().replace(",", "")) for match in _NUMBER.finditer(text))


def explicit_number(text: str) -> Decimal | None:
    """The number a label prints outright, or ``None``: no inference, no partial reads.

    A trailing ``%`` and an accounting negative ``(130)`` are printed forms of the number
    itself; anything the whole (comma-grouped) pattern does not match is not a number here.
    """
    label = text.strip().removesuffix("%").strip()
    negative = label.startswith("(") and label.endswith(")")
    if negative:
        label = label[1:-1].strip()
        if label.startswith(("+", "-")):
            return None
    if _NUMBER.fullmatch(label) is None:
        return None
    number = Decimal(label.replace(",", ""))
    return -number if negative else number


def validate_svg(svg: SvgArtifact) -> None:
    """Validate immutable element references, source anchors and readable XML labels."""
    if not svg.figure_id or "<!DOCTYPE" in svg.svg or "<!ENTITY" in svg.svg:
        raise FigureError(
            FailureCode.INVALID_EVIDENCE,
            "SVG identity is missing or XML contains unsupported declarations",
        )
    try:
        root = ElementTree.fromstring(svg.svg)
    except ElementTree.ParseError as error:
        raise FigureError(FailureCode.INVALID_EVIDENCE, "SVG is not well-formed XML") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise FigureError(FailureCode.INVALID_EVIDENCE, "Figure representation must be SVG")
    nodes = tuple(node for node in root.iter() if node.get("id"))
    ids = tuple(node.get("id") for node in nodes)
    mapped_ids = tuple(element.element_id for element in svg.elements)
    if not mapped_ids or len(set(ids)) != len(ids) or len(set(mapped_ids)) != len(mapped_ids):
        raise FigureError(
            FailureCode.INVALID_EVIDENCE,
            "SVG and source-map element IDs must be unique and nonempty",
        )
    lookup = {node.get("id"): node for node in nodes}
    source = svg.source
    for element in svg.elements:
        node = lookup.get(element.element_id)
        if node is None or _normal(element.text) != _normal("".join(node.itertext())):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Source-map text differs from its actual SVG element",
            )
        observation = node.tag == "{urn:enterprise-pdf-rag:source-observation-v1}observation"
        if observation != (element.evidence_kind is EvidenceKind.SOURCE_TEXT_OBSERVATION):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Source observation is not a native glyph mapping",
            )
        if observation and (
            node.get("source-span-id") != element.source_span_id
            or element.text_range is None
            or (node.get("start"), node.get("end"))
            != tuple(str(value) for value in element.text_range)
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Source observation offsets or identity differ",
            )
        anchor = element.anchor
        if (
            anchor.source_revision,
            anchor.document_sha256,
            anchor.page_index,
            anchor.coordinate_frame,
            anchor.rotation,
            anchor.transform,
        ) != (
            source.source_revision,
            source.document_sha256,
            source.page_index,
            source.coordinate_frame,
            source.rotation,
            source.transform,
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Element anchor points to a different PDF source or coordinate frame",
            )
        if not (
            source.bbox[0] <= anchor.bbox[0] < anchor.bbox[2] <= source.bbox[2]
            and source.bbox[1] <= anchor.bbox[1] < anchor.bbox[3] <= source.bbox[3]
        ):
            raise FigureError(
                FailureCode.INVALID_EVIDENCE,
                "Element source bbox is outside the figure region",
            )


def evidence_elements(svg: SvgArtifact, evidence: Evidence) -> tuple[SvgElement, ...]:
    elements = {element.element_id: element for element in svg.elements}
    if any(element_id not in elements for element_id in evidence.element_ids):
        raise FigureError(FailureCode.INVALID_EVIDENCE, "Evidence refers to a missing SVG element")
    return tuple(elements[element_id] for element_id in evidence.element_ids)


def _validate_text(svg: SvgArtifact, field: TextField) -> None:
    elements = evidence_elements(svg, field.evidence)
    texts = tuple(_normal(element.text) for element in elements)
    if not field.text.strip() or _normal(field.text) not in (*texts, " ".join(texts)):
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "Field label is not present in its cited SVG elements",
        )


def validate_pair(svg: SvgArtifact, chart: ChartIR, description: TextDescription) -> None:
    """Check cited source labels, then cross-check independently produced claims."""
    for contextual in (chart.title, chart.period):
        if contextual is not None:
            _validate_text(svg, contextual)
    for axis in chart.axes:
        _validate_text(svg, axis.label)
        _validate_text(svg, axis.unit)
    for point in chart.points:
        for field in (point.series, point.category, point.unit):
            _validate_text(svg, field)
        elements = evidence_elements(svg, point.value.evidence)
        if point.value.kind is ValueKind.EXPLICIT and point.value.value not in tuple(
            explicit_number(element.text) for element in elements
        ):
            raise FigureError(
                FailureCode.CONTENT_MISMATCH,
                "Explicit numeric value is absent from its cited source label",
            )
    for claim in description.claims:
        _validate_claim(svg, chart, claim)


def _validate_claim(svg: SvgArtifact, chart: ChartIR, claim: DescriptionClaim) -> None:
    elements = evidence_elements(svg, claim.evidence)
    if not claim.text.strip() or claim.text.lstrip().startswith(("{", "[", "<", "ChartIR(")):
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "Embedding input must be a natural-language description, not structured serialization",
        )
    if claim.value is None:
        if _normal(claim.text).rstrip(".") not in tuple(
            _normal(element.text).rstrip(".") for element in elements
        ):
            raise FigureError(
                FailureCode.CONTENT_MISMATCH,
                "The first slice permits nonnumeric claims only as grounded label transcriptions",
            )
        return
    candidates = tuple(
        point
        for point in chart.points
        if (
            point.series.text == claim.series
            and point.category.text == claim.category
            and point.unit.text == claim.unit
            and point.value.value == claim.value
        )
    )
    if len(candidates) != 1:
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "Claim number, series, category and unit must match one chart point",
        )
    point = candidates[0]
    period = None if chart.period is None else chart.period.text
    if claim.period != period:
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "Claim period differs from the chart source scope",
        )
    if point.value.kind is not ValueKind.EXPLICIT:
        raise FigureError(
            FailureCode.UNSUPPORTED_VALUE,
            "Only verified explicit labels support exact claims in this slice",
        )
    required = {
        element_id
        for field in (point.series, point.category, point.unit, point.value)
        for element_id in field.evidence.element_ids
    }
    if chart.period is not None:
        required.update(chart.period.evidence.element_ids)
    if not required.issubset(claim.evidence.element_ids):
        raise FigureError(
            FailureCode.INVALID_EVIDENCE,
            "Claim must cite its matched point and all contextual labels",
        )
    numeric_text = claim.text
    for label in (
        claim.series,
        claim.category,
        claim.unit,
        *((claim.period,) if claim.period is not None else ()),
    ):
        if label is None or label not in numeric_text:
            raise FigureError(
                FailureCode.CONTENT_MISMATCH,
                "Claim text omits or changes a bound contextual label",
            )
        numeric_text = numeric_text.replace(label, "")
    if _numbers(numeric_text) != (claim.value,):
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "Claim text contains a different or additional numeric value",
        )
    sentence = rf"{re.escape(str(claim.series))} for {re.escape(str(claim.category))}: ({_NUMBER.pattern}) {re.escape(str(claim.unit))}\."
    if (
        chart.grammar == "donut"
        and chart.title is not None
        and chart.title.text == "Distribution Mix"
        and " distribution share for " in claim.text
    ):
        sentence = rf"{re.escape(str(claim.series))} distribution share for {re.escape(str(claim.category))}: ({_NUMBER.pattern}) {re.escape(str(claim.unit))}\."
    if claim.period is not None:
        sentence = rf"During {re.escape(claim.period)}, " + sentence
    if re.fullmatch(sentence, _normal(claim.text)) is None:
        raise FigureError(
            FailureCode.CONTENT_MISMATCH,
            "This slice validates only positive label statements; free-form trends and negation require a separate semantic verifier",
        )
