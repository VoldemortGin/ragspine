"""Chart answers consume qualified fields and retain every operand's evidence."""

from dataclasses import replace
from decimal import Decimal, localcontext
from hashlib import sha256
from xml.etree import ElementTree

import pytest

from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_reasoning import prepare_figure
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    ChartQuestion,
    Operation,
    PointSelector,
    QueryPin,
    QueryStatus,
    RefusalReason,
)
from enterprise_pdf_rag.figures.chart_qa.service import ChartQAService
from enterprise_pdf_rag.figures.models import (
    Evidence,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.models import PageInput
from tests.enterprise_pdf_rag.adapters.test_donut_qualification import sample


class PinnedResolver:
    def __init__(self, context: ChartContext) -> None:
        self.context = context
        self.calls: list[QueryPin] = []

    def resolve(self, pin: QueryPin) -> ChartContext:
        self.calls.append(pin)
        return self.context


def qualified_context() -> ChartContext:
    prepared, raw_chart, raw_description = sample()
    pair = DonutQualification(prepared).qualify_pair(prepared.svg, raw_chart, raw_description)
    return ChartContext(
        QueryPin("a" * 64, "b" * 64, "c" * 64),
        "d" * 64,
        pair.chart,
        pair.description,
        pair.receipt,
        prepared.svg,
    )


def question(context: ChartContext) -> ChartQuestion:
    point = context.chart.points[0]
    return ChartQuestion(
        context.pin,
        Operation.LOOKUP,
        "VONB",
        "1H26",
        "%",
        (PointSelector(point.point_id, point.category.text),),
    )


def test_lookup_preserves_explicit_display_and_each_field_occurrence() -> None:
    context = qualified_context()
    resolver = PinnedResolver(context)
    result = ChartQAService(resolver).answer(question(context))

    assert result.status is QueryStatus.ANSWERED
    assert result.answer is not None
    assert result.answer.value == Decimal("72")
    assert result.answer.unit == "%"
    assert result.answer.value_kind is ValueKind.EXPLICIT
    assert result.answer.raw_display == "72%"
    assert result.answer.verification is Verification.VERIFIED
    assert result.answer.confidence.score is None
    assert result.answer.confidence.method.startswith("source-qualified explicit percentage")
    assert result.calculation_receipt is None
    assert result.refusal_reason is None
    assert result.pin == context.pin
    assert resolver.calls == [context.pin]
    claim = result.inputs[0]
    assert (claim.series, claim.category, claim.period) == ("VONB", "Agency", "1H26")
    assert {c.field_path for c in claim.citations} == {
        f"points.{claim.point_id}.series",
        f"points.{claim.point_id}.category",
        f"points.{claim.point_id}.unit",
        f"points.{claim.point_id}.value",
        "period",
    }
    for citation in claim.citations:
        assert citation.chart_ir_artifact_id == context.chart.artifact_id
        assert citation.svg_artifact_id == context.svg.artifact_id
        assert citation.qualification_id == context.qualification.artifact_id
        for occurrence in citation.occurrences:
            assert occurrence in context.svg.elements
            assert occurrence.anchor.document_sha256 == context.svg.source.document_sha256


def test_difference_is_ordered_decimal_calculation_not_a_source_literal() -> None:
    context = qualified_context()
    query = replace(
        question(context),
        operation=Operation.PERCENTAGE_POINT_DIFFERENCE,
        points=tuple(PointSelector(p.point_id, p.category.text) for p in context.chart.points),
    )
    with localcontext() as hostile:
        hostile.prec = 1
        result = ChartQAService(PinnedResolver(context)).answer(query)
    assert result.status is QueryStatus.ANSWERED
    assert result.answer is not None
    assert result.answer.value == Decimal("44")
    assert result.answer.unit == "percentage_points"
    assert result.answer.value_kind is ValueKind.DERIVED
    assert result.answer.raw_display is None
    assert result.answer.verification is Verification.VERIFIED
    assert result.answer.confidence.score is None
    assert result.answer.confidence.method.startswith("deterministic Decimal subtraction")
    receipt = result.calculation_receipt
    assert receipt is not None
    assert receipt.inputs[0].value == Decimal("72")
    assert receipt.inputs[1].value == Decimal("28")
    assert receipt.output_value == Decimal("44")
    assert receipt.output_unit == "percentage_points"
    assert receipt.precision == 64
    assert receipt.rounding == "ROUND_HALF_EVEN"
    assert receipt.source_refs
    assert context.chart.points[0].value.kind is ValueKind.EXPLICIT

    reverse = ChartQAService(PinnedResolver(context)).answer(
        replace(query, points=tuple(reversed(query.points)))
    )
    assert reverse.answer is not None
    assert reverse.answer.value == Decimal("-44")
    assert reverse.calculation_receipt is not None
    assert reverse.calculation_receipt.artifact_id != receipt.artifact_id


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("series", "Revenue", RefusalReason.SERIES_MISMATCH),
        ("period", "1H25", RefusalReason.PERIOD_MISMATCH),
        ("unit", "HK cents", RefusalReason.UNIT_MISMATCH),
    ),
)
def test_wrong_semantic_request_abstains(field: str, value: str, reason: RefusalReason) -> None:
    context = qualified_context()
    query = question(context)
    query = replace(
        query,
        series=value if field == "series" else query.series,
        period=value if field == "period" else query.period,
        unit=value if field == "unit" else query.unit,
    )
    result = ChartQAService(PinnedResolver(context)).answer(query)
    assert result.status is QueryStatus.ABSTAINED
    assert result.refusal_reason is reason
    assert result.answer is None and not result.inputs
    assert result.calculation_receipt is None


def test_valid_point_id_does_not_authorize_another_category() -> None:
    context = qualified_context()
    query = question(context)
    query = replace(query, points=(replace(query.points[0], category="Partnerships"),))
    result = ChartQAService(PinnedResolver(context)).answer(query)
    assert result.refusal_reason is RefusalReason.CATEGORY_MISMATCH
    assert result.answer is None


@pytest.mark.parametrize("kind", (ValueKind.ESTIMATED, ValueKind.DERIVED, ValueKind.UNAVAILABLE))
def test_nonexplicit_inputs_cannot_become_exact_answers(kind: ValueKind) -> None:
    context = qualified_context()
    point = context.chart.points[0]
    changed = replace(
        point,
        value=replace(
            point.value,
            kind=kind,
            value=None if kind is ValueKind.UNAVAILABLE else point.value.value,
        ),
    )
    context = replace(
        context,
        chart=replace(context.chart, points=(changed, *context.chart.points[1:])),
    )
    result = ChartQAService(PinnedResolver(context)).answer(question(context))
    assert result.refusal_reason is RefusalReason.UNSUPPORTED_VALUE_KIND
    assert result.answer is None


def test_label_scope_is_not_promoted_even_with_numeric_chart_fields() -> None:
    context = qualified_context()
    context = replace(
        context,
        qualification=replace(context.qualification, semantic_scope="figure-source-labels-only-v1"),
    )
    result = ChartQAService(PinnedResolver(context)).answer(question(context))
    assert result.refusal_reason is RefusalReason.UNQUALIFIED_MEMBER


def test_receipt_scope_and_verified_flags_cannot_authorize_false_value() -> None:
    context = qualified_context()
    point = context.chart.points[0]
    context = replace(
        context,
        chart=replace(
            context.chart,
            points=(
                replace(point, value=replace(point.value, value=Decimal("73"))),
                *context.chart.points[1:],
            ),
        ),
    )
    with pytest.raises(ChartQueryError, match="evidence"):
        ChartQAService(PinnedResolver(context)).answer(question(context))


def test_receipt_wrong_occurrence_and_pending_field_fail_closed() -> None:
    context = qualified_context()
    first, *others = context.qualification.fields
    bad_receipt = (
        replace(
            context.qualification,
            fields=(
                replace(first, element_ids=context.chart.period.evidence.element_ids),
                *others,
            ),
        )
        if context.chart.period is not None
        else context.qualification
    )
    with pytest.raises(ChartQueryError, match="every exact chart field"):
        ChartQAService(PinnedResolver(replace(context, qualification=bad_receipt))).answer(
            question(context)
        )
    point = context.chart.points[0]
    changed = replace(
        point,
        value=replace(
            point.value,
            evidence=replace(point.value.evidence, verification=Verification.PENDING),
        ),
    )
    with pytest.raises(ChartQueryError, match="every exact chart field"):
        ChartQAService(
            PinnedResolver(
                replace(
                    context,
                    chart=replace(context.chart, points=(changed, *context.chart.points[1:])),
                )
            )
        ).answer(question(context))


def test_resolver_cannot_switch_snapshot_or_source() -> None:
    context = qualified_context()
    other = replace(context, pin=replace(context.pin, snapshot_id="f" * 64))
    with pytest.raises(ChartQueryError, match="another immutable membership"):
        ChartQAService(PinnedResolver(other)).answer(question(context))
    other = replace(
        context,
        qualification=replace(
            context.qualification,
            source=replace(context.qualification.source, page_index=0),
        ),
    )
    with pytest.raises(ChartQueryError, match="binding differs"):
        ChartQAService(PinnedResolver(other)).answer(question(context))


def test_zero_difference_is_a_derived_zero_with_both_ordered_inputs() -> None:
    context = qualified_context()
    query = question(context)
    query = replace(
        query,
        operation=Operation.PERCENTAGE_POINT_DIFFERENCE,
        points=(query.points[0], query.points[0]),
    )
    result = ChartQAService(PinnedResolver(context)).answer(query)
    assert result.answer is not None and result.answer.value == Decimal(0)
    assert result.answer.value_kind is ValueKind.DERIVED
    assert result.calculation_receipt is not None
    assert len(result.calculation_receipt.inputs) == 2


def fractional_context(left: str, right: str) -> ChartContext:
    old, raw_chart, raw_description = sample()
    replacements = {"72": left, "28": right, "72%": left + "%", "28%": right + "%"}
    spans = tuple(
        replace(span, text=replacements.get(span.text, span.text)) for span in old.paint_text_spans
    )
    native = old.crop_svg.split(">", 1)[1][:-6].encode()
    prepared = prepare_figure(
        page=PageInput(
            old.view.source_manifest_id,
            old.svg.source.document_sha256,
            17,
            240.0,
            160.0,
            AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
            TextSidecar("source-text-v1", old.svg.source.document_sha256, 17, spans),
        ),
        native_svg=native,
        bbox=old.svg.source.bbox,
        region_id=old.svg.figure_id,
    )
    remap = {
        element.element_id: next(
            new.element_id
            for new in prepared.svg.elements
            if new.source_span_id == element.source_span_id
            and new.text == replacements.get(element.text, element.text)
        )
        for element in old.svg.elements
    }

    def evidence(value: Evidence) -> Evidence:
        return replace(
            value,
            element_ids=tuple(remap[element_id] for element_id in value.element_ids),
        )

    def field(value: TextField | None) -> TextField | None:
        return None if value is None else replace(value, evidence=evidence(value.evidence))

    points = tuple(
        replace(
            point,
            series=replace(point.series, evidence=evidence(point.series.evidence)),
            category=replace(point.category, evidence=evidence(point.category.evidence)),
            unit=replace(point.unit, evidence=evidence(point.unit.evidence)),
            value=replace(
                point.value,
                value=Decimal(value),
                evidence=evidence(point.value.evidence),
            ),
        )
        for point, value in zip(raw_chart.points, (left, right), strict=True)
    )
    chart = replace(
        raw_chart,
        binding=prepared.svg.binding,
        points=points,
        title=field(raw_chart.title),
        period=field(raw_chart.period),
        marks=(),
    )
    description = replace(
        raw_description,
        binding=prepared.svg.binding,
        claims=tuple(
            replace(
                claim,
                evidence=evidence(claim.evidence),
                value=None
                if claim.value is None
                else Decimal(replacements.get(str(claim.value), str(claim.value))),
                text=claim.text.replace("72", left).replace("28", right),
            )
            for claim in raw_description.claims
        ),
    )
    pair = DonutQualification(prepared).qualify_pair(prepared.svg, chart, description)
    original = qualified_context()
    return replace(
        original,
        chart=pair.chart,
        description=pair.description,
        qualification=pair.receipt,
        svg=prepared.svg,
    )


def test_fractional_subtraction_is_exact_and_preserves_source_precision() -> None:
    context = fractional_context("72.125", "27.875")
    query = replace(
        question(context),
        operation=Operation.PERCENTAGE_POINT_DIFFERENCE,
        points=tuple(
            PointSelector(point.point_id, point.category.text) for point in context.chart.points
        ),
    )
    with localcontext() as global_context:
        global_context.prec = 1
        global_context.rounding = "ROUND_DOWN"
        result = ChartQAService(PinnedResolver(context)).answer(query)
    assert result.answer is not None and result.answer.value == Decimal("44.250")
    assert tuple(claim.raw_display for claim in result.inputs) == ("72.125%", "27.875%")
    assert result.calculation_receipt is not None
    assert tuple(ref.bbox for ref in result.calculation_receipt.source_refs) == tuple(
        claim.citations[-1].occurrences[0].anchor.bbox for claim in result.inputs
    )


def test_more_than_28_fractional_places_abstains_instead_of_rounding() -> None:
    context = fractional_context("72." + "0" * 28 + "1", "28")
    result = ChartQAService(PinnedResolver(context)).answer(question(context))
    assert result.refusal_reason is RefusalReason.UNSUPPORTED_PRECISION
    assert result.answer is None


def test_explicit_decimal_display_is_preserved_without_fabricated_formatting() -> None:
    context = fractional_context("72.0", "28.0")
    result = ChartQAService(PinnedResolver(context)).answer(question(context))
    assert result.answer is not None
    assert result.answer.value.as_tuple().exponent == -1
    assert result.answer.raw_display == "72.0%"


@pytest.mark.parametrize("prefix", (">", "约"))
def test_qualified_numeric_substring_cannot_hide_comparison_or_estimate(
    prefix: str,
) -> None:
    context = qualified_context()
    whole = next(element for element in context.svg.elements if element.text == "72%")
    assert whole.source_span_id is not None
    changed = tuple(
        replace(element, text=prefix + element.text, text_range=(0, 4))
        if element == whole
        else replace(element, text_range=(element.text_range[0] + 1, element.text_range[1] + 1))
        if element.source_span_id == whole.source_span_id and element.text_range is not None
        else element
        for element in context.svg.elements
    )
    # The qualifier must reject this source; even a faulty resolver must not let
    # a genuine substring "72%" erase the source occurrence's prefix.
    substring = replace(whole, element_id="numeric-substring", text_range=(1, 4))
    changed = (*changed, substring)
    root = ElementTree.fromstring(context.svg.svg)
    nodes = {node.get("id"): node for node in root.iter() if node.get("id")}
    for element in changed:
        if element.element_id == substring.element_id:
            node = ElementTree.SubElement(
                root, "{urn:enterprise-pdf-rag:source-observation-v1}observation"
            )
            node.set("id", element.element_id)
            node.set("source-span-id", whole.source_span_id)
        else:
            node = nodes[element.element_id]
        node.text = element.text
        assert element.text_range is not None
        node.set("start", str(element.text_range[0]))
        node.set("end", str(element.text_range[1]))
    svg = replace(
        context.svg,
        svg=ElementTree.tostring(root, encoding="unicode"),
        elements=changed,
    )
    context = replace(
        context,
        svg=svg,
        chart=replace(context.chart, binding=svg.binding),
        description=replace(context.description, binding=svg.binding),
        qualification=replace(context.qualification, binding=svg.binding),
    )
    with pytest.raises(ChartQueryError, match="percentage display"):
        ChartQAService(PinnedResolver(context)).answer(question(context))
