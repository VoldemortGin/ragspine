"""Only explicit, visible direct labels qualify; raw branches remain intact."""

from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_pdf_rag.adapters.bar_qualification import qualify_displayed_bar
from enterprise_pdf_rag.adapters.description_normalization import (
    normalize_description_evidence,
)
from enterprise_pdf_rag.adapters.source_paint_bar import build_bar_source_paint_proof
from ragspine.extraction.evidence.figures.models import Verification
from tests.enterprise_pdf_rag.adapters.bar_source_fixture import bar_source


def test_authored_source_qualifies_two_independent_claims_without_global_period() -> None:
    source = bar_source()
    normalized = normalize_description_evidence(
        svg=source.prepared.svg,
        raw_json=source.raw_description,
        previous=source.previous_description,
    )
    proof = build_bar_source_paint_proof(source.pdf, prepared=source.prepared)
    result = qualify_displayed_bar(
        source.prepared,
        raw_chart=source.chart,
        description=normalized.description,
        source_proof=proof,
    )
    assert tuple(point.value.value for point in result.chart.points) == (
        Decimal("15"),
        Decimal("6"),
    )
    assert result.chart.period is None
    assert result.chart.verification is Verification.PENDING
    assert tuple(claim.text for claim in result.description.claims) == (
        "Expense Ratio for 1H21: 15 %.",
        "Expense Ratio for 1H23: 6 %.",
    )
    assert tuple(period.literal for period in result.point_periods) == ("1H21", "1H23")
    assert result.qualification.semantic_scope == "displayed-percent-bar-lookup-v1"
    assert source.chart.points[1].value.value is None
    assert source.chart.verification is Verification.PENDING


@pytest.mark.parametrize("case", ["swap", "height_guess", "commentary", "source"])
def test_explicit_scope_rejects_mismatched_values_and_unproved_conclusions(
    case: str,
) -> None:
    source = bar_source()
    description = normalize_description_evidence(
        svg=source.prepared.svg,
        raw_json=source.raw_description,
        previous=source.previous_description,
    ).description
    chart = source.chart
    proof = build_bar_source_paint_proof(source.pdf, prepared=source.prepared)
    if case == "swap":
        chart = replace(
            chart,
            points=(
                replace(chart.points[0], value=chart.points[2].value),
                chart.points[1],
                chart.points[2],
            ),
        )
    elif case == "height_guess":
        chart = replace(
            chart,
            points=(
                chart.points[0],
                replace(chart.points[1], value=chart.points[0].value),
                chart.points[2],
            ),
        )
    elif case == "commentary":
        description = replace(
            description,
            claims=(
                replace(
                    description.claims[0],
                    text=description.claims[0].text + " This proves market leadership.",
                ),
                *description.claims[1:],
            ),
        )
    else:
        proof = replace(proof, source_sha256="0" * 64)
    with pytest.raises(
        ValueError,
        match=r"bar_explicit_value|unlabelled_bar|unqualified_bar_semantics|bar_source_proof",
    ):
        qualify_displayed_bar(
            source.prepared,
            raw_chart=chart,
            description=description,
            source_proof=proof,
        )
