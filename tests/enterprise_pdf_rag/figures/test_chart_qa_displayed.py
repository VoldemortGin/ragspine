"""A trusted resolver supplies displayed bar facts, never height estimates."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from xml.etree import ElementTree

import pytest

from enterprise_pdf_rag.adapters.figure_reasoning import prepare_figure
from ragspine.extraction.evidence.document.models import AssetRef, TextSidecar, TextSpan
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import (
    DescriptionNormalizationCitation,
    DisplayedLookupContext,
    DisplayedRefusalReason,
    PageContextCitation,
    PointPeriodInterpretation,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_service import (
    DisplayedChartQAService,
)
from ragspine.extraction.evidence.figures.chart_qa.models import (
    ChartQueryError,
    ChartQuestion,
    Operation,
    PointSelector,
    QueryFailure,
    QueryPin,
    QueryStatus,
)
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    ExecutionMode,
    FieldOccurrence,
    FigureQualification,
    NumericObservation,
    SourceAnchor,
    SvgElement,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from ragspine.extraction.evidence.page.models import PageInput


class DisplayedResolver:
    """Fake trusted source port; source geometry is covered in adapter tests."""

    def __init__(self, context: DisplayedLookupContext) -> None:
        self.context = context
        self.calls: list[QueryPin] = []

    def resolve(self, pin: QueryPin) -> DisplayedLookupContext:
        self.calls.append(pin)
        return self.context


def displayed_context(*, first_display: str = "8.2%") -> DisplayedLookupContext:
    native = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160" viewBox="0 0 240 160"/>'
    )
    source = sha256(b"authored source port fixture").hexdigest()
    labels = (
        ("Expense Ratio", (30.0, 5.0, 130.0, 15.0)),
        ("8.2%", (10.0, 30.0, 40.0, 40.0)),
        ("6.9%", (180.0, 50.0, 210.0, 60.0)),
        ("1H24", (10.0, 110.0, 40.0, 120.0)),
        ("1H25", (90.0, 110.0, 120.0, 120.0)),
        ("1H26", (180.0, 110.0, 210.0, 120.0)),
    )
    sidecar = TextSidecar(
        "source-text-v1",
        source,
        19,
        tuple(TextSpan(f"source-{i}", text, bbox) for i, (text, bbox) in enumerate(labels)),
    )
    page = PageInput(
        "d" * 64,
        source,
        19,
        240.0,
        160.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        sidecar,
    )
    prepared = prepare_figure(
        page=page,
        native_svg=native,
        bbox=(0.0, 0.0, 240.0, 130.0),
        region_id="authored-expense-ratio",
    )
    svg = prepared.svg
    if first_display != "8.2%":
        root = ElementTree.fromstring(svg.svg)
        offset = first_display.index("8.2")
        elements: list[SvgElement] = []
        for element in svg.elements:
            if element.source_span_id == "source-1":
                assert element.text_range is not None
                updated_range = (
                    (0, len(first_display))
                    if element.text == "8.2%"
                    else tuple(v + offset for v in element.text_range)
                )
                element = replace(
                    element,
                    text=first_display if element.text == "8.2%" else element.text,
                    text_range=(updated_range[0], updated_range[1]),
                )
                node = next(node for node in root.iter() if node.get("id") == element.element_id)
                node.text = element.text
                node.set("start", str(updated_range[0]))
                node.set("end", str(updated_range[1]))
            elements.append(element)
        svg = replace(
            svg,
            svg=ElementTree.tostring(root, encoding="unicode"),
            elements=tuple(elements),
        )
    by_text = {item.text: item for item in svg.elements}
    confidence = Confidence(None, "source-qualified explicit field occurrence")

    def evidence(text: str) -> Evidence:
        return Evidence((by_text[text].element_id,), Verification.PENDING, confidence)

    points: list[ChartPoint] = []
    for category, number in (("1H24", "8.2"), ("1H25", None), ("1H26", "6.9")):
        unit = next(
            item
            for item in svg.elements
            if item.text == "%" and item.source_span_id == by_text[number or "8.2"].source_span_id
        )
        points.append(
            ChartPoint(
                f"point-{category.lower()}",
                TextField("Expense Ratio", evidence("Expense Ratio")),
                TextField(category, evidence(category)),
                TextField("%", Evidence((unit.element_id,), Verification.PENDING, confidence)),
                NumericObservation(
                    None if number is None else Decimal(number),
                    ValueKind.UNAVAILABLE if number is None else ValueKind.EXPLICIT,
                    evidence(category if number is None else number),
                ),
            )
        )
    raw_chart = ChartIR(
        svg.binding,
        "bar",
        (),
        tuple(points),
        "independent-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        TextField("Expense Ratio", evidence("Expense Ratio")),
    )

    def verified(ev: Evidence) -> Evidence:
        return replace(ev, verification=Verification.VERIFIED)

    approved = tuple(
        replace(
            point,
            series=replace(point.series, evidence=verified(point.series.evidence)),
            category=replace(point.category, evidence=verified(point.category.evidence)),
            unit=replace(point.unit, evidence=verified(point.unit.evidence)),
            value=replace(point.value, evidence=verified(point.value.evidence)),
        )
        for point in (points[0], points[2])
    )
    title = TextField("Expense Ratio", verified(evidence("Expense Ratio")))
    chart = replace(
        raw_chart,
        producer="source-displayed-projection-v1",
        points=approved,
        title=title,
    )
    description = TextDescription(
        svg.binding,
        tuple(
            DescriptionClaim(
                f"Expense Ratio for {point.category.text}: {point.value.value} %.",
                Evidence(
                    tuple(
                        dict.fromkeys(
                            (
                                *point.series.evidence.element_ids,
                                *point.category.evidence.element_ids,
                                *point.value.evidence.element_ids,
                                *point.unit.evidence.element_ids,
                            )
                        )
                    ),
                    Verification.VERIFIED,
                    confidence,
                ),
                "Expense Ratio",
                point.category.text,
                "%",
                point.value.value,
                None,
            )
            for point in approved
        ),
        "source-qualified-independent-description",
        Verification.VERIFIED,
        ExecutionMode.PRODUCTION,
    )
    fields = [FieldOccurrence("title", title.evidence.element_ids)]
    for point in approved:
        fields.extend(
            FieldOccurrence(f"points.{point.point_id}.{name}", ev.element_ids)
            for name, ev in (
                ("series", point.series.evidence),
                ("category", point.category.evidence),
                ("unit", point.unit.evidence),
                ("value", point.value.evidence),
            )
        )
    receipt = FigureQualification(
        svg.binding,
        svg.source,
        tuple(fields),
        "fake trusted source bar port",
        ExecutionMode.PRODUCTION,
        ("trusted-proof-v3:fixture",),
        "displayed-percent-bar-lookup-v1",
    )
    periods = tuple(
        PointPeriodInterpretation(
            svg.binding,
            raw_chart.artifact_id,
            point.point_id,
            f"points.{point.point_id}.category",
            point.category.text,
            point.category.evidence,
            "point-category-period-v1",
            Verification.VERIFIED,
            Confidence(None, "source category supplies point period role only"),
        )
        for point in approved
    )
    footnote = (
        "Expense ratio comparatives and two-year changes are shown on an actual exchange rate basis"
    )
    context = PageContextCitation(
        page.source_manifest_id,
        "e" * 64,
        "source-footnote",
        footnote,
        SourceAnchor(
            source,
            source,
            19,
            (10.0, 140.0, 230.0, 150.0),
            coordinate_frame=svg.source.coordinate_frame,
        ),
        (0, len(footnote)),
        Verification.VERIFIED,
        Confidence(None, "source-qualified page context occurrence; no financial inference"),
    )
    return DisplayedLookupContext(
        QueryPin("a" * 64, "b" * 64, "c" * 64),
        page.source_manifest_id,
        raw_chart,
        chart,
        description,
        receipt,
        svg,
        periods,
        (context,),
        DescriptionNormalizationCitation(
            svg.binding,
            "description-normalization-v1:" + "1" * 64,
            "2" * 64,
            "description-v2:" + "3" * 64,
            "description-v2:" + "4" * 64,
            "duplicate-evidence-normalization-v1",
        ),
    )


def displayed_question(context: DisplayedLookupContext) -> ChartQuestion:
    return ChartQuestion(
        context.pin,
        Operation.LOOKUP,
        "Expense Ratio",
        "1H24",
        "%",
        (PointSelector("point-1h24", "1H24"),),
    )


def test_display_lookup_cites_raw_category_as_period_without_global_period() -> None:
    context = displayed_context()
    resolver = DisplayedResolver(context)
    result = DisplayedChartQAService(resolver).answer(displayed_question(context))
    assert result.status is QueryStatus.ANSWERED
    assert result.semantic_scope == "source_display_only"
    assert result.answer is not None
    assert result.answer.value == Decimal("8.2")
    assert result.answer.raw_display == "8.2%"
    assert result.answer.value_kind is ValueKind.EXPLICIT
    assert result.answer.verification is Verification.VERIFIED
    assert result.answer.confidence.score is None
    assert result.calculation_receipt is None
    assert context.raw_chart.period is None and context.chart.period is None
    assert context.chart.verification is Verification.PENDING
    claim = result.inputs[0]
    assert claim.period_interpretation == context.point_periods[0]
    fields = {item.role: item for item in claim.citations}
    assert set(fields) == {"series", "category", "period", "unit", "value"}
    assert fields["period"].raw_field_path == "points.point-1h24.category"
    assert fields["period"].raw_chart_ir_artifact_id == context.raw_chart.artifact_id
    assert fields["period"].citation == fields["category"].citation
    assert result.page_context == context.page_context
    assert not any(item.element_id == "source-footnote" for item in context.svg.elements)
    assert resolver.calls == [context.pin]


def test_known_missing_period_refuses_instead_of_estimating_from_other_bars() -> None:
    context = displayed_context()
    query = replace(
        displayed_question(context),
        period="1H25",
        points=(PointSelector("point-1h25", "1H25"),),
    )
    result = DisplayedChartQAService(DisplayedResolver(context)).answer(query)
    assert result.status is QueryStatus.ABSTAINED
    assert result.refusal_reason is DisplayedRefusalReason.VALUE_UNAVAILABLE
    assert result.answer is None and not result.inputs
    assert result.calculation_receipt is None


def test_scope_or_verified_flags_cannot_replace_exact_raw_and_receipt_bindings() -> None:
    context = displayed_context()
    point = context.chart.points[0]
    neighbor = context.chart.points[1]
    forged = replace(point, value=neighbor.value, unit=neighbor.unit)
    changed = replace(
        context, chart=replace(context.chart, points=(forged, context.chart.points[1]))
    )
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(changed)).answer(displayed_question(context))
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE


def test_cross_period_difference_is_unsupported_even_with_two_explicit_values() -> None:
    context = displayed_context()
    query = replace(
        displayed_question(context),
        operation=Operation.PERCENTAGE_POINT_DIFFERENCE,
        points=(
            PointSelector("point-1h24", "1H24"),
            PointSelector("point-1h26", "1H26"),
        ),
    )
    resolver = DisplayedResolver(context)
    result = DisplayedChartQAService(resolver).answer(query)
    assert result.refusal_reason is DisplayedRefusalReason.UNSUPPORTED_OPERATION
    assert result.answer is None and result.calculation_receipt is None
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("series", "Revenue", DisplayedRefusalReason.SERIES_MISMATCH),
        ("period", "1H26", DisplayedRefusalReason.PERIOD_MISMATCH),
        ("unit", "bps", DisplayedRefusalReason.UNIT_MISMATCH),
        ("category", "1H26", DisplayedRefusalReason.CATEGORY_MISMATCH),
        ("point_id", "arrow", DisplayedRefusalReason.UNKNOWN_POINT),
    ),
)
def test_displayed_lookup_rejects_mismatched_requested_roles(
    field: str,
    value: str,
    reason: DisplayedRefusalReason,
) -> None:
    context = displayed_context()
    query = displayed_question(context)
    query = replace(
        query,
        series=value if field == "series" else query.series,
        period=value if field == "period" else query.period,
        unit=value if field == "unit" else query.unit,
        points=(
            replace(
                query.points[0],
                category=value if field == "category" else query.points[0].category,
                point_id=value if field == "point_id" else query.points[0].point_id,
            ),
        ),
    )
    result = DisplayedChartQAService(DisplayedResolver(context)).answer(query)
    assert result.refusal_reason is reason
    assert result.answer is None and not result.inputs


@pytest.mark.parametrize(
    "fault",
    (
        "receipt_occurrence",
        "missing_receipt_field",
        "raw_binding",
        "projection_period",
        "raw_period",
        "period_role_path",
        "period_role_raw_id",
        "period_role_literal",
        "period_role_confidence",
        "missing_period_role",
        "duplicate_period_role",
        "page_context_source",
        "page_context_crop",
        "page_context_text_range",
        "page_context_pending",
        "missing_page_context",
        "pending_value",
        "extra_point",
    ),
)
def test_incomplete_or_misbound_qualification_never_returns_a_number(
    fault: str,
) -> None:
    context = displayed_context()
    role = context.point_periods[0]
    footnote = context.page_context[0]
    match fault:
        case "receipt_occurrence":
            fields = context.qualification.fields
            changed = replace(
                fields[1],
                element_ids=context.chart.points[1].value.evidence.element_ids,
            )
            context = replace(
                context,
                qualification=replace(
                    context.qualification, fields=(fields[0], changed, *fields[2:])
                ),
            )
        case "missing_receipt_field":
            context = replace(
                context,
                qualification=replace(
                    context.qualification, fields=context.qualification.fields[1:]
                ),
            )
        case "raw_binding":
            context = replace(
                context,
                raw_chart=replace(
                    context.raw_chart,
                    binding=replace(context.raw_chart.binding, svg_digest="f" * 64),
                ),
            )
        case "projection_period":
            context = replace(
                context,
                chart=replace(context.chart, period=context.chart.points[0].category),
            )
        case "raw_period":
            context = replace(
                context,
                raw_chart=replace(context.raw_chart, period=context.raw_chart.points[0].category),
            )
        case "period_role_path":
            context = replace(
                context,
                point_periods=(
                    replace(role, raw_field_path="period"),
                    context.point_periods[1],
                ),
            )
        case "period_role_raw_id":
            context = replace(
                context,
                point_periods=(
                    replace(role, raw_chart_ir_artifact_id="chart-v2:other"),
                    context.point_periods[1],
                ),
            )
        case "period_role_literal":
            context = replace(
                context,
                point_periods=(replace(role, literal="1H26"), context.point_periods[1]),
            )
        case "period_role_confidence":
            context = replace(
                context,
                point_periods=(
                    replace(role, confidence=Confidence(Decimal(1), "invented certainty")),
                    context.point_periods[1],
                ),
            )
        case "missing_period_role":
            context = replace(context, point_periods=())
        case "duplicate_period_role":
            context = replace(context, point_periods=(role, role))
        case "page_context_source":
            context = replace(
                context,
                page_context=(replace(footnote, source=replace(footnote.source, page_index=18)),),
            )
        case "page_context_crop":
            context = replace(context, page_context=(replace(footnote, source=context.svg.source),))
        case "page_context_text_range":
            context = replace(
                context,
                page_context=(replace(footnote, text_range=(1, len(footnote.text))),),
            )
        case "page_context_pending":
            context = replace(
                context,
                page_context=(replace(footnote, verification=Verification.PENDING),),
            )
        case "missing_page_context":
            context = replace(context, page_context=())
        case "pending_value":
            point = context.chart.points[0]
            point = replace(
                point,
                value=replace(
                    point.value,
                    evidence=replace(point.value.evidence, verification=Verification.PENDING),
                ),
            )
            context = replace(
                context,
                chart=replace(context.chart, points=(point, context.chart.points[1])),
            )
        case "extra_point":
            context = replace(
                context,
                chart=replace(
                    context.chart,
                    points=(*context.chart.points, context.raw_chart.points[1]),
                ),
            )
        case _:
            raise AssertionError(fault)
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE


def test_cross_snapshot_port_result_is_a_pin_conflict() -> None:
    context = displayed_context()
    query = replace(displayed_question(context), pin=replace(context.pin, snapshot_id="f" * 64))
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(context)).answer(query)
    assert caught.value.code is QueryFailure.PIN_CONFLICT


def test_old_label_only_receipt_cannot_authorize_display_lookup() -> None:
    context = displayed_context()
    context = replace(
        context,
        qualification=replace(context.qualification, semantic_scope="figure-source-labels-only-v1"),
    )
    result = DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert result.refusal_reason is DisplayedRefusalReason.UNQUALIFIED_MEMBER
    assert result.answer is None


@pytest.mark.parametrize("display", (">8.2%", "<8.2%", "~8.2%", "about 8.2%"))
def test_numeric_substring_cannot_hide_approximation_or_comparator(
    display: str,
) -> None:
    context = displayed_context(first_display=display)
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE


@pytest.mark.parametrize("kind", (ValueKind.ESTIMATED, ValueKind.DERIVED, ValueKind.UNAVAILABLE))
def test_nonexplicit_projection_is_not_a_displayed_number(kind: ValueKind) -> None:
    context = displayed_context()
    point = context.chart.points[0]
    point = replace(
        point,
        value=replace(
            point.value,
            kind=kind,
            value=None if kind is ValueKind.UNAVAILABLE else point.value.value,
        ),
    )
    context = replace(
        context, chart=replace(context.chart, points=(point, context.chart.points[1]))
    )
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE


def test_second_explicit_value_remains_decimal_source_display() -> None:
    context = displayed_context()
    query = replace(
        displayed_question(context),
        period="1H26",
        points=(PointSelector("point-1h26", "1H26"),),
    )
    result = DisplayedChartQAService(DisplayedResolver(context)).answer(query)
    assert result.answer is not None
    assert result.answer.value == Decimal("6.9")
    assert result.answer.raw_display == "6.9%"
    assert result.inputs[0].period_interpretation == context.point_periods[1]


def test_raw_explicit_point_without_qualified_projection_is_a_refusal() -> None:
    context = displayed_context()
    context = replace(
        context,
        chart=replace(context.chart, points=(context.chart.points[1],)),
        description=replace(context.description, claims=(context.description.claims[1],)),
        qualification=replace(
            context.qualification,
            fields=tuple(
                field
                for field in context.qualification.fields
                if not field.field_path.startswith("points.point-1h24.")
            ),
        ),
        point_periods=(context.point_periods[1],),
    )
    result = DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert result.status is QueryStatus.ABSTAINED
    assert result.refusal_reason is DisplayedRefusalReason.INSUFFICIENT_EVIDENCE
    assert result.answer is None


@pytest.mark.parametrize("fault", ("rule", "raw_hash", "typed_id", "binding"))
def test_normalization_audit_must_name_real_typed_branch_identities(fault: str) -> None:
    context = displayed_context()
    provenance = context.description_normalization
    if fault == "rule":
        provenance = replace(provenance, rule_version="trust-model-v1")
    elif fault == "raw_hash":
        provenance = replace(provenance, raw_response_sha256="unknown")
    elif fault == "typed_id":
        provenance = replace(provenance, normalized_description_artifact_id="unavailable")
    else:
        provenance = replace(provenance, binding=replace(provenance.binding, svg_digest="f" * 64))
    context = replace(context, description_normalization=provenance)
    with pytest.raises(ChartQueryError) as caught:
        DisplayedChartQAService(DisplayedResolver(context)).answer(displayed_question(context))
    assert caught.value.code is QueryFailure.INVALID_EVIDENCE
