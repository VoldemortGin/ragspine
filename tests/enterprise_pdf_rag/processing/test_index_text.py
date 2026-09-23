"""Chart members index a projection of their qualified IR; other members their description."""

from dataclasses import replace
from decimal import Decimal

from enterprise_pdf_rag.processing.index_text import (
    chart_index_text,
    diagram_index_text,
    formula_index_text,
    has_citable_structure,
    has_citable_value,
    member_index_text,
)
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    ChartPoint,
    Confidence,
    Evidence,
    NumericObservation,
    SourceAnchor,
    SvgBinding,
    TextField,
    ValueKind,
    Verification,
)
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaStructure,
    FormulaToken,
    PathEvidence,
    StructureKind,
    TokenRole,
)
from ragspine.extraction.evidence.objects.typed_ir import (
    DiagramEdge,
    DiagramIR,
    DiagramNode,
    FormulaIR,
    TextIR,
)

_SHA = "c" * 64
_BINDING = SvgBinding("fig", _SHA, "svg-v2:" + "e" * 64, "f" * 64)
_TITLE = "Distribution Mix"


def _evidence(*ids: str) -> Evidence:
    return Evidence(ids, Verification.VERIFIED, Confidence(None, "fixture"))


def _point(point_id: str, category: str, value: Decimal | None, *, unit: str = "%") -> ChartPoint:
    observation = (
        NumericObservation(None, ValueKind.UNAVAILABLE, _evidence(f"e-{point_id}"))
        if value is None
        else NumericObservation(value, ValueKind.EXPLICIT, _evidence(f"e-{point_id}"))
    )
    return ChartPoint(
        point_id,
        TextField("VONB", _evidence("e-series")),
        TextField(category, _evidence(f"e-cat-{point_id}")),
        TextField(unit, _evidence(f"e-unit-{point_id}")),
        observation,
    )


def _donut(*points: ChartPoint, verification: Verification = Verification.VERIFIED) -> ChartIR:
    return ChartIR(
        _BINDING,
        "donut",
        (),
        points,
        "fixture",
        verification,
        title=TextField(_TITLE, _evidence("e-title")),
        period=TextField("1H26", _evidence("e-period")),
    )


def test_verified_chart_projects_title_period_grammar_and_every_explicit_point() -> None:
    chart = _donut(
        _point("point-agency", "Agency", Decimal("72")),
        _point("point-partnerships", "Partnerships", Decimal("28")),
    )
    assert has_citable_value(chart)
    text = chart_index_text(chart, fallback=_TITLE)
    assert text == "Distribution Mix 1H26 donut chart figure Agency VONB 72% Partnerships VONB 28%"
    # Deterministic: the same IR always yields the same text.
    assert chart_index_text(chart, fallback="other") == text
    assert member_index_text(chart, _TITLE) == text


def test_charts_without_a_citable_value_keep_their_description_text() -> None:
    label_only = _donut(verification=Verification.PENDING)
    assert not has_citable_value(label_only)
    assert chart_index_text(label_only, fallback=_TITLE) == _TITLE

    unavailable_only = _donut(_point("point-agency", "Agency", None))
    assert not has_citable_value(unavailable_only)
    assert chart_index_text(unavailable_only, fallback=_TITLE) == _TITLE
    assert "Agency" not in member_index_text(unavailable_only, _TITLE)

    derived = replace(
        _point("point-agency", "Agency", Decimal("72")),
        value=NumericObservation(Decimal("72"), ValueKind.DERIVED, _evidence("e-derived")),
    )
    assert chart_index_text(_donut(derived), fallback=_TITLE) == _TITLE


def test_unavailable_points_keep_labels_but_no_value_and_word_units_stay_separate() -> None:
    chart = replace(
        _donut(
            _point("p-1h21", "1H21", Decimal("15"), unit="US cents"),
            _point("p-1h22", "1H22", None),
        ),
        grammar="bar",
        title=None,
        period=None,
    )
    assert chart_index_text(chart, fallback="x") == (
        "bar chart figure 1H21 VONB 15 US cents 1H22 VONB"
    )


def _diagram(*, labelled: bool = True, edges: tuple[DiagramEdge, ...] = ()) -> DiagramIR:
    anchor = SourceAnchor(_SHA, _SHA, 2, (15.0, 65.0, 225.0, 105.0))
    nodes = (
        DiagramNode("n2", "BUILD" if labelled else "", (150.0, 70.0, 220.0, 100.0), ("sp-build",)),
        DiagramNode("n1", "PLAN" if labelled else "", (20.0, 70.0, 90.0, 100.0), ("sp-plan",)),
    )
    return DiagramIR("diagram-1", anchor, nodes, edges, ())


def test_proven_diagram_projects_labels_in_reading_order_and_every_edge() -> None:
    edge = DiagramEdge("n1", "n2", None, "leads to")
    diagram = _diagram(edges=(edge,))
    assert has_citable_structure(diagram)
    text = diagram_index_text(diagram, fallback="Two boxes joined by an arrow.")
    # Reading order, not IR order: the left frame comes first on the same row.
    assert text == "diagram figure PLAN BUILD PLAN -> BUILD"
    assert diagram_index_text(diagram, fallback="other") == text
    assert member_index_text(diagram, "Two boxes joined by an arrow.") == text
    nodes_only = _diagram()
    assert diagram_index_text(nodes_only, fallback="x") == "diagram figure PLAN BUILD"


def test_diagram_without_source_labels_keeps_its_description_text() -> None:
    fallback = "Five tiles and two curved arrows."
    unlabelled = _diagram(labelled=False)
    assert not has_citable_structure(unlabelled)
    assert diagram_index_text(unlabelled, fallback=fallback) == fallback
    assert member_index_text(unlabelled, fallback) == fallback
    anchor = SourceAnchor(_SHA, _SHA, 2, (15.0, 65.0, 225.0, 105.0))
    empty = DiagramIR("diagram-1", anchor, (), (), ())
    assert diagram_index_text(empty, fallback=fallback) == fallback


_LINEAR = "ROE = \\frac{Net\\ profit}{Equity}"
_READABLE = "ROE 等于 Net profit 除以 Equity"


def _formula(*, proven: bool = True) -> FormulaIR:
    """The authored ``ROE = Net profit / Equity`` fixture, already proven from its source."""
    anchor = SourceAnchor(_SHA, _SHA, 2, (18.0, 56.0, 132.0, 100.0))
    if not proven:
        return FormulaIR("formula-1", anchor, "ROE =", "ROE = x", ("sp-roe",), ())
    tokens = (
        FormulaToken(0, "ROE", "sp-roe", 0, 3, (20.0, 72.4, 41.6, 84.4), TokenRole.OPERAND),
        FormulaToken(1, "=", "sp-roe", 4, 5, (48.8, 72.4, 56.0, 84.4), TokenRole.RELATION),
        FormulaToken(2, "Net", "sp-num", 0, 3, (62.0, 65.2, 81.8, 76.2), TokenRole.OPERAND),
        FormulaToken(3, "profit", "sp-num", 4, 10, (88.4, 65.2, 128.0, 76.2), TokenRole.OPERAND),
        FormulaToken(4, "Equity", "sp-den", 0, 6, (72.0, 85.2, 111.6, 96.2), TokenRole.OPERAND),
    )
    structure = FormulaStructure(
        StructureKind.FRACTION,
        PathEvidence(0, "line", ((60.0, 78.0), (120.0, 78.0)), 0.8),
        (2, 3),
        (4,),
    )
    return FormulaIR(
        "formula-1",
        anchor,
        "ROE =\nNet profit\nEquity",
        None,
        ("sp-roe", "sp-num", "sp-den"),
        ("proof_level=full",),
        Verification.VERIFIED,
        tokens,
        (structure,),
        _LINEAR,
        _READABLE,
        "full",
    )


def test_formula_members_index_readable_linear_and_tokens() -> None:
    formula = _formula()
    expected = f"{_READABLE} {_LINEAR} formula ROE = Net profit Equity"
    assert formula_index_text(formula, fallback="A ratio definition.") == expected
    assert member_index_text(formula, "A ratio definition.") == expected


def test_model_only_formula_ir_keeps_description_text() -> None:
    fallback = "A formula defining ROE as Net profit over Equity."
    model_only = _formula(proven=False)
    assert model_only.tokens == ()
    assert formula_index_text(model_only, fallback=fallback) == fallback
    assert member_index_text(model_only, fallback) == fallback


def test_literal_members_index_their_description_unchanged() -> None:
    anchor = SourceAnchor(_SHA, _SHA, 0, (0.0, 0.0, 10.0, 10.0))
    assert member_index_text(TextIR("t", anchor, ()), "Revenue grew") == "Revenue grew"
