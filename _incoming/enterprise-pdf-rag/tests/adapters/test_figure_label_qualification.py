"""Figure label qualification promotes exact source labels and nothing semantic."""

from dataclasses import replace

import pytest
from tests.adapters.test_donut_qualification import sample

from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_LABEL_SCOPE,
    qualify_source_labels,
)
from enterprise_pdf_rag.figures.models import (
    Confidence,
    DescriptionClaim,
    Evidence,
    FigureError,
    Verification,
)


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

    result = qualify_source_labels(prepared.svg, chart, description)

    assert result.chart is not chart
    assert result.chart.verification is Verification.PENDING
    assert result.chart.grammar == chart.grammar
    assert result.chart.axes == ()
    assert result.chart.points == ()
    assert result.chart.marks == ()
    assert result.chart.title is result.chart.period is None
    assert result.chart.producer.startswith("source-labels-only-unknown-chart-v1:")
    assert tuple(claim.text for claim in result.description.claims) == (
        "Distribution Mix",
    )
    assert all(
        (claim.series, claim.category, claim.unit, claim.value, claim.period)
        == (None, None, None, None, None)
        for claim in result.description.claims
    )
    assert result.description.verification is Verification.VERIFIED
    assert all(
        claim.evidence.verification is Verification.VERIFIED
        for claim in result.description.claims
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
        qualify_source_labels(prepared.svg, chart, description)


def test_chart_fields_never_generate_or_change_label_text() -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"),))
    first = qualify_source_labels(prepared.svg, chart, description)
    changed_chart = replace(chart, grammar="line", title=None, points=())
    second = qualify_source_labels(prepared.svg, changed_chart, description)

    assert first.description == second.description
    assert first.receipt == second.receipt
    assert first.raw_chart_id != second.raw_chart_id
    assert first.chart.points == second.chart.points == ()


def test_one_source_occurrence_is_indexed_at_most_once() -> None:
    prepared, chart, raw = sample()
    title = _claim(prepared, "Distribution Mix")
    description = replace(raw, claims=(title, title))

    result = qualify_source_labels(prepared.svg, chart, description)

    assert tuple(claim.text for claim in result.description.claims) == (
        "Distribution Mix",
    )
    assert result.excluded_claim_paths == ("claims[1]:duplicate_label_occurrence",)


def test_label_text_cannot_hide_a_number_inside_a_phrase() -> None:
    prepared, chart, raw = sample()
    agency = _claim(prepared, "Agency")
    description = replace(raw, claims=(replace(agency, text="Agency 72%"),))

    with pytest.raises(FigureError, match="no_exact_source_labels"):
        qualify_source_labels(prepared.svg, chart, description)


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
        qualify_source_labels(svg, chart, description)


def test_different_svg_binding_fails_before_projecting_text() -> None:
    prepared, chart, raw = sample()
    description = replace(raw, claims=(_claim(prepared, "Distribution Mix"),))
    rebound = replace(
        description, binding=replace(description.binding, figure_id="other")
    )
    with pytest.raises(FigureError, match="binding"):
        qualify_source_labels(prepared.svg, chart, rebound)
