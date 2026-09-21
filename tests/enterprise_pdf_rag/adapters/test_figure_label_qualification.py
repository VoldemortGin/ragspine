"""Figure label qualification promotes exact source labels and nothing semantic."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

import pytest

from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_LABEL_SCOPE,
    FIGURE_POINT_SCOPE,
    QualifiedLabelProjection,
    qualify_source_labels,
)
from enterprise_pdf_rag.adapters.figure_reasoning import prepare_figure
from enterprise_pdf_rag.documents.models import AssetRef, TextSidecar, TextSpan
from enterprise_pdf_rag.figures.models import (
    ChartAxis,
    ChartIR,
    ChartPoint,
    Confidence,
    DescriptionClaim,
    Evidence,
    ExecutionMode,
    FigureError,
    NumericObservation,
    SvgArtifact,
    TextDescription,
    TextField,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.index_text import chart_index_text, has_citable_value
from enterprise_pdf_rag.processing.models import PageInput
from tests.enterprise_pdf_rag.adapters.test_donut_qualification import sample


def _claim(prepared: object, text: str, *, field: bool = False) -> DescriptionClaim:
    svg = prepared.svg  # type: ignore[attr-defined]
    element = next(item for item in svg.elements if item.text == text)
    return DescriptionClaim(
        text,
        Evidence(
            (element.element_id,),
            Verification.PENDING,
            Confidence(None, "model-declared high; uncalibrated"),
        ),
        "model-series" if field else None,
        "model-category" if field else None,
        "model-unit" if field else None,
        None,
        "model-period" if field else None,
    )


def test_only_exact_nonnumeric_source_occurrences_become_qualified_labels() -> None:
    prepared, chart, raw = sample()
    labels = (
        _claim(prepared, "Distribution Mix", field=True),
        _claim(prepared, "1H26", field=True),
        _claim(prepared, "72%"),
        *raw.claims,
    )
    description = replace(raw, claims=labels)

    result = qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)

    assert result.chart is not chart
    assert result.chart.verification is Verification.PENDING
    assert result.chart.grammar == chart.grammar
    assert result.chart.axes == ()
    assert result.chart.points == ()
    assert result.chart.marks == ()
    assert result.chart.title is result.chart.period is None
    assert result.chart.producer.startswith("source-labels-only-unknown-chart-v1:")
    assert tuple(claim.text for claim in result.description.claims) == ("Distribution Mix",)
    assert all(
        (claim.series, claim.category, claim.unit, claim.value, claim.period)
        == (None, None, None, None, None)
        for claim in result.description.claims
    )
    assert result.description.verification is Verification.VERIFIED
    assert all(
        claim.evidence.verification is Verification.VERIFIED for claim in result.description.claims
    )
    assert result.receipt.semantic_scope == FIGURE_LABEL_SCOPE
    assert result.receipt.execution_mode == "production"
    assert len(result.receipt.fields) == 1
    assert result.excluded_claim_paths == (
        "claims[1]:numeric_text_not_a_label",
        "claims[2]:numeric_text_not_a_label",
        "claims[3]:numeric_claim_not_a_label",
        "claims[4]:numeric_claim_not_a_label",
    )
    assert result.raw_chart_id == chart.artifact_id
    assert result.raw_description_id == description.artifact_id


def test_unknown_rejected_or_constructed_evidence_cannot_become_a_label() -> None:
    prepared, chart, raw = sample()
    title = _claim(prepared, "Distribution Mix")
    agency = _claim(prepared, "Agency")
    constructed = DescriptionClaim(
        "Distribution Mix Agency",
        Evidence(
            (*title.evidence.element_ids, *agency.evidence.element_ids),
            Verification.PENDING,
            Confidence(None, "model"),
        ),
    )
    missing = replace(
        title,
        evidence=replace(title.evidence, element_ids=("missing-element",)),
    )
    rejected = replace(
        title,
        evidence=replace(title.evidence, verification=Verification.REJECTED),
    )
    description = replace(raw, claims=(constructed, missing, rejected))

    with pytest.raises(FigureError, match="no_exact_source_labels"):
        qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)


def test_chart_fields_never_generate_or_change_label_text() -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"),))
    first = qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)
    changed_chart = replace(chart, grammar="line", title=None, points=())
    second = qualify_source_labels(
        prepared.svg, changed_chart, description, scope=FIGURE_LABEL_SCOPE
    )

    assert first.description == second.description
    assert first.receipt == second.receipt
    assert first.raw_chart_id != second.raw_chart_id
    assert first.chart.points == second.chart.points == ()


def test_one_source_occurrence_is_indexed_at_most_once() -> None:
    prepared, chart, raw = sample()
    title = _claim(prepared, "Distribution Mix")
    description = replace(raw, claims=(title, title))

    result = qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)

    assert tuple(claim.text for claim in result.description.claims) == ("Distribution Mix",)
    assert result.excluded_claim_paths == ("claims[1]:duplicate_label_occurrence",)


def test_label_text_cannot_hide_a_number_inside_a_phrase() -> None:
    prepared, chart, raw = sample()
    agency = _claim(prepared, "Agency")
    description = replace(raw, claims=(replace(agency, text="Agency 72%"),))

    with pytest.raises(FigureError, match="no_exact_source_labels"):
        qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)


@pytest.mark.parametrize("rejected", ["svg", "chart", "description"])
def test_rejected_or_rebound_inputs_fail_closed(rejected: str) -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"),))
    svg = prepared.svg
    if rejected == "svg":
        svg = replace(svg, verification=Verification.REJECTED)
    elif rejected == "chart":
        chart = replace(chart, verification=Verification.REJECTED)
    else:
        description = replace(description, verification=Verification.REJECTED)
    with pytest.raises(FigureError, match=r"rejected|binding"):
        qualify_source_labels(svg, chart, description, scope=FIGURE_LABEL_SCOPE)


def test_different_svg_binding_fails_before_projecting_text() -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"),))
    rebound = replace(description, binding=replace(description.binding, figure_id="other"))
    with pytest.raises(FigureError, match="binding"):
        qualify_source_labels(prepared.svg, chart, rebound, scope=FIGURE_LABEL_SCOPE)


# --------------------------------------------------------------------------------------
# source-labels-and-verbatim-points-v1
# --------------------------------------------------------------------------------------

Printed = tuple[str, tuple[float, float, float, float]]


def _figure(*printed: Printed) -> SvgArtifact:
    """An authored figure that prints exactly ``printed``, one text span per entry."""
    digest = sha256(repr(printed).encode()).hexdigest()
    sidecar = TextSidecar(
        "source-text-v1",
        digest,
        3,
        tuple(
            TextSpan(f"source-{index}", text, bbox) for index, (text, bbox) in enumerate(printed)
        ),
    )
    native = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="700" height="600" viewBox="0 0 700 600">'
        b'<rect x="10" y="10" width="40" height="40" fill="#d31145"/></svg>'
    )
    page = PageInput(
        "a" * 64,
        digest,
        3,
        700.0,
        600.0,
        AssetRef(sha256(native).hexdigest(), "image/svg+xml", len(native)),
        sidecar,
    )
    return prepare_figure(
        page=page, native_svg=native, bbox=(0.0, 0.0, 700.0, 600.0), region_id="authored-figure"
    ).svg


def _evidence(svg: SvgArtifact, *texts: str) -> Evidence:
    elements = {element.text: element for element in svg.elements}
    return Evidence(
        tuple(elements[text].element_id for text in texts),
        Verification.PENDING,
        Confidence(None, "model-declared high; uncalibrated"),
    )


def _field(svg: SvgArtifact, text: str, *cites: str) -> TextField:
    return TextField(text, _evidence(svg, *(cites or (text,))))


def _point(
    svg: SvgArtifact,
    point_id: str,
    category: TextField,
    series: TextField,
    unit: TextField,
    value: Decimal | None,
    *value_cites: str,
) -> ChartPoint:
    observation = NumericObservation(
        value,
        ValueKind.EXPLICIT if value is not None else ValueKind.UNAVAILABLE,
        _evidence(svg, *value_cites),
    )
    return ChartPoint(point_id, series, category, unit, observation)


def _chart(
    svg: SvgArtifact,
    *,
    points: tuple[ChartPoint, ...] = (),
    title: TextField | None = None,
    period: TextField | None = None,
    axes: tuple[ChartAxis, ...] = (),
    grammar: str = "bar",
) -> ChartIR:
    return ChartIR(
        svg.binding,
        grammar,
        axes,
        points,
        "independent-chart-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
        title,
        period,
    )


def _description(svg: SvgArtifact, *claims: DescriptionClaim) -> TextDescription:
    return TextDescription(
        svg.binding,
        claims,
        "independent-description-model",
        Verification.PENDING,
        ExecutionMode.PRODUCTION,
    )


def _label(svg: SvgArtifact, text: str, *cites: str) -> DescriptionClaim:
    return DescriptionClaim(text, _evidence(svg, *(cites or (text,))))


def _texts(result: object) -> tuple[str, ...]:
    projection: QualifiedLabelProjection = result  # type: ignore[assignment]
    return tuple(claim.text for claim in projection.description.claims)


def test_a_point_qualifies_when_its_value_and_unit_print_in_one_span() -> None:
    svg = _figure(
        ("Expense Ratio", (100.0, 10.0, 180.0, 22.0)),
        ("1H24", (100.0, 80.0, 130.0, 92.0)),
        ("8.2%", (100.0, 40.0, 130.0, 52.0)),
    )
    point = _point(
        svg,
        "point-1h24",
        _field(svg, "1H24"),
        _field(svg, "Expense Ratio"),
        TextField("%", _evidence(svg, "%")),
        Decimal("8.2"),
        "8.2%",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "Expense Ratio"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "Expense Ratio")))

    (kept,) = result.chart.points
    assert kept.value.value == Decimal("8.2") and kept.value.kind is ValueKind.EXPLICIT
    assert kept.unit.text == "%"
    # The one window printed both, so the unit needs no window of its own.
    assert kept.unit.evidence.element_ids == kept.value.evidence.element_ids
    assert kept.category.text == "1H24"
    assert kept.value.evidence.verification is Verification.VERIFIED
    assert result.chart.verification is Verification.PENDING
    assert result.chart.marks == ()
    assert result.chart.producer.startswith("verbatim-source-points-v1:")


def test_a_point_qualifies_when_its_value_and_unit_print_in_adjacent_spans() -> None:
    svg = _figure(
        ("VONB", (100.0, 10.0, 140.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("937", (100.0, 40.0, 124.0, 52.0)),
        ("$m", (125.0, 40.0, 140.0, 52.0)),
    )
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "VONB"),
        _field(svg, "$m"),
        Decimal("937"),
        "937",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "VONB"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "VONB")))

    (kept,) = result.chart.points
    assert kept.value.value == Decimal("937")
    assert kept.unit.text == "$m"
    # Each field carries only the window that printed it; the receipt closes over all four.
    assert kept.unit.evidence.element_ids != kept.value.evidence.element_ids
    proved = {field.field_path: field.element_ids for field in result.receipt.fields}
    assert proved["points.point-1h26.value"] == kept.value.evidence.element_ids
    assert proved["points.point-1h26.unit"] == kept.unit.evidence.element_ids
    assert proved["points.point-1h26.category"] == kept.category.evidence.element_ids
    assert proved["points.point-1h26.series"] == kept.series.evidence.element_ids


def test_a_unit_may_be_read_out_of_the_charts_own_axis_caption() -> None:
    svg = _figure(
        ("VONB ($m)", (100.0, 10.0, 180.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("937", (100.0, 40.0, 124.0, 52.0)),
    )
    axis = ChartAxis("axis-y", _field(svg, "VONB ($m)"), _field(svg, "VONB ($m)"), "linear")
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "VONB ($m)"),
        # The model cites the wrong occurrence for the unit; the axis caption still prints it.
        TextField("$m", _evidence(svg, "1H26")),
        Decimal("937"),
        "937",
    )
    chart = _chart(svg, points=(point,), axes=(axis,))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "VONB ($m)")))

    (kept,) = result.chart.points
    assert kept.unit.text == "$m"
    (axis_element,) = _evidence(svg, "VONB ($m)").element_ids
    assert kept.unit.evidence.element_ids == (axis_element,)


def test_a_unit_that_prints_nowhere_in_the_figure_drops_its_point() -> None:
    svg = _figure(
        ("VONB", (100.0, 10.0, 140.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("937", (100.0, 40.0, 124.0, 52.0)),
    )
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "VONB"),
        TextField("$m", _evidence(svg, "VONB")),
        Decimal("937"),
        "937",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "VONB"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "VONB")))

    assert result.chart.points == ()
    assert "points.point-1h26:unit_is_not_printed_in_the_figure" in result.excluded_claim_paths


def test_a_wrapped_category_qualifies_its_point() -> None:
    svg = _figure(
        ("Traditional", (397.4, 239.3, 454.2, 251.0)),
        ("Protection", (397.4, 251.9, 449.5, 263.6)),
        ("33%", (367.2, 247.1, 388.3, 258.9)),
        ("VONB", (327.8, 261.4, 353.7, 271.4)),
    )
    point = _point(
        svg,
        "point-traditional-protection",
        TextField("Traditional Protection", _evidence(svg, "Traditional", "Protection")),
        _field(svg, "VONB"),
        TextField("%", _evidence(svg, "%")),
        Decimal("33"),
        "33%",
    )
    chart = _chart(svg, points=(point,), grammar="donut")

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "VONB")))

    (kept,) = result.chart.points
    assert kept.category.text == "Traditional Protection"
    assert len(kept.category.evidence.element_ids) == 2
    assert "Traditional Protection" in _texts(result)


def test_a_verbatim_label_that_contains_digits_is_admitted() -> None:
    svg = _figure(
        ("VONB", (100.0, 10.0, 140.0, 22.0)),
        ("1H26", (100.0, 30.0, 130.0, 42.0)),
        ("+9%", (100.0, 50.0, 126.0, 62.0)),
    )
    chart = _chart(svg, period=_field(svg, "1H26"), title=_field(svg, "VONB"))
    description = _description(svg, _label(svg, "1H26"), _label(svg, "+9%"))

    result = qualify_source_labels(svg, chart, description)

    assert _texts(result) == ("1H26", "+9%", "VONB")
    # v1 refuses both on sight: any digit made a claim "not a label" there.
    with pytest.raises(FigureError, match="no_exact_source_labels"):
        qualify_source_labels(svg, chart, description, scope=FIGURE_LABEL_SCOPE)


def test_one_trailing_parenthetical_may_be_dropped_to_match_the_print() -> None:
    svg = _figure(
        ("UFSG per share", (100.0, 10.0, 200.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("15", (100.0, 40.0, 116.0, 52.0)),
    )
    title = TextField("UFSG per share (US cents)", _evidence(svg, "UFSG per share"))
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        title,
        TextField("", _evidence(svg, "15")),
        Decimal("15"),
        "15",
    )
    chart = _chart(svg, points=(point,), title=title)

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "1H26")))

    assert result.chart.title is not None
    # The source text is recorded, never the model's longer string.
    assert result.chart.title.text == "UFSG per share"
    (kept,) = result.chart.points
    assert kept.series.text == "UFSG per share" and kept.unit.text == ""


def test_a_value_that_prints_nowhere_drops_only_its_own_point() -> None:
    svg = _figure(
        ("Expense Ratio", (100.0, 10.0, 180.0, 22.0)),
        ("1H24", (100.0, 80.0, 130.0, 92.0)),
        ("1H25", (140.0, 80.0, 170.0, 92.0)),
        ("8.2%", (100.0, 40.0, 130.0, 52.0)),
    )
    good = _point(
        svg,
        "point-1h24",
        _field(svg, "1H24"),
        _field(svg, "Expense Ratio"),
        TextField("%", _evidence(svg, "%")),
        Decimal("8.2"),
        "8.2%",
    )
    invented = _point(
        svg,
        "point-1h25",
        _field(svg, "1H25"),
        _field(svg, "Expense Ratio"),
        TextField("%", _evidence(svg, "%")),
        Decimal("7.4"),
        "8.2%",
    )
    chart = _chart(svg, points=(good, invented), title=_field(svg, "Expense Ratio"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "Expense Ratio")))

    assert tuple(point.point_id for point in result.chart.points) == ("point-1h24",)
    assert "points.point-1h25:value_is_not_printed_in_the_figure" in result.excluded_claim_paths


def test_an_unavailable_value_never_becomes_a_point() -> None:
    svg = _figure(
        ("Expense Ratio", (100.0, 10.0, 180.0, 22.0)),
        ("1H25", (140.0, 80.0, 170.0, 92.0)),
    )
    point = _point(
        svg,
        "point-1h25",
        _field(svg, "1H25"),
        _field(svg, "Expense Ratio"),
        TextField("%", _evidence(svg, "1H25")),
        None,
        "1H25",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "Expense Ratio"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "Expense Ratio")))

    assert result.chart.points == ()
    assert "points.point-1h25:value_is_not_explicit_in_source" in result.excluded_claim_paths


def test_a_thousands_separated_number_is_read_from_the_span_that_prints_it() -> None:
    svg = _figure(
        ("VONB", (100.0, 10.0, 140.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("1,234", (100.0, 40.0, 134.0, 52.0)),
    )
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "VONB"),
        TextField("", _evidence(svg, "1,234")),
        Decimal("1234"),
        "1,234",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "VONB"))

    result = qualify_source_labels(svg, chart, _description(svg, _label(svg, "VONB")))

    (kept,) = result.chart.points
    assert kept.value.value == Decimal("1234")
    printed = next(element for element in svg.elements if element.text == "1,234")
    assert kept.value.evidence.element_ids == (printed.element_id,)
    assert any(
        ref.endswith(printed.element_id) and ref.startswith("points.point-1h26.value")
        for ref in result.receipt.source_geometry_refs
    )


def test_kept_points_reach_the_index_text_projection() -> None:
    prepared, chart, raw = sample()

    result = qualify_source_labels(prepared.svg, chart, raw)

    assert chart_index_text(result.chart, fallback=result.description.text) == (
        "Distribution Mix 1H26 donut chart figure Agency VONB 72% Partnerships VONB 28%"
    )
    assert has_citable_value(result.chart)
    assert _texts(result) == ("Distribution Mix", "1H26", "Agency", "VONB", "Partnerships")
    assert result.receipt.semantic_scope == FIGURE_POINT_SCOPE
    assert result.receipt.method.startswith("source-labels-and-verbatim-points-v1;")


def test_the_v1_scope_still_blanks_every_point_of_the_same_figure() -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"), *raw.claims))

    v1 = qualify_source_labels(prepared.svg, chart, description, scope=FIGURE_LABEL_SCOPE)

    assert v1.chart.points == () and v1.chart.title is None
    assert v1.chart.producer.startswith("source-labels-only-unknown-chart-v1:")
    assert chart_index_text(v1.chart, fallback=v1.description.text) == v1.description.text


def test_constructed_non_adjacent_evidence_is_still_refused() -> None:
    prepared, chart, raw = sample()
    elements = {element.text: element for element in prepared.svg.elements}
    constructed = DescriptionClaim(
        "Distribution Mix Agency",
        Evidence(
            (elements["Distribution Mix"].element_id, elements["Agency"].element_id),
            Verification.PENDING,
            Confidence(None, "model"),
        ),
    )
    description = replace(raw, claims=(constructed,))

    result = qualify_source_labels(prepared.svg, chart, description)

    assert "Distribution Mix Agency" not in _texts(result)
    assert "claims[0]:not_an_adjacent_source_occurrence" in result.excluded_claim_paths


def test_a_figure_that_prints_none_of_its_strings_fails_closed() -> None:
    svg = _figure(("Expense Ratio", (100.0, 10.0, 180.0, 22.0)))
    invented = TextField("Invented Caption", _evidence(svg, "Expense Ratio"))
    chart = _chart(svg, title=invented)
    description = _description(svg, DescriptionClaim("Invented Caption", invented.evidence))

    with pytest.raises(FigureError, match="no_exact_source_labels"):
        qualify_source_labels(svg, chart, description)


def test_an_undeclared_label_policy_cannot_qualify_anything() -> None:
    prepared, chart, raw = sample()

    with pytest.raises(FigureError, match="unknown_label_qualification_scope"):
        qualify_source_labels(prepared.svg, chart, raw, scope="self-promoted-labels-v9")
