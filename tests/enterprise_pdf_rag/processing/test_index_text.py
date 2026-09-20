"""Chart members index a projection of their qualified IR; other members their description."""

from dataclasses import replace
from decimal import Decimal

from enterprise_pdf_rag.figures.models import (
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
from enterprise_pdf_rag.processing.index_text import (
    chart_index_text,
    has_citable_value,
    member_index_text,
)
from enterprise_pdf_rag.processing.typed_ir import TextIR

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


def test_literal_members_index_their_description_unchanged() -> None:
    anchor = SourceAnchor(_SHA, _SHA, 0, (0.0, 0.0, 10.0, 10.0))
    assert member_index_text(TextIR("t", anchor, ()), "Revenue grew") == "Revenue grew"
