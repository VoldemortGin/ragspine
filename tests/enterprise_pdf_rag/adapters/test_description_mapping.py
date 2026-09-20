"""Description mapping keeps valid claims while locating unsafe model fields."""

from decimal import Decimal

import pytest

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    DescriptionClaimDTO,
    EvidenceDTO,
    FigureDescriptionDTO,
)
from enterprise_pdf_rag.adapters.description_mapping import map_description
from enterprise_pdf_rag.figures.models import Confidence, ExecutionMode, Verification
from tests.enterprise_pdf_rag.adapters.test_chart_semantics import _prepared


def _claim(
    *,
    element_ids: tuple[str, ...],
    confidence: str | None = "source text observation",
    value: str | None = None,
    text: str = "VONB",
) -> DescriptionClaimDTO:
    return DescriptionClaimDTO(
        text=text,
        evidence=EvidenceDTO(element_ids=element_ids, confidence=confidence),
        series=None,
        category=None,
        unit=None,
        value=value,
        period=None,
    )


def test_mapping_keeps_valid_claim_when_another_claim_has_unknown_evidence() -> None:
    prepared = _prepared()
    vonb = next(element for element in prepared.svg.elements if element.text == "VONB")
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=prepared.svg.digest,
        claims=(
            _claim(element_ids=(vonb.element_id,)),
            _claim(element_ids=("missing-element",), text="invented"),
        ),
        diagnostics=(),
    )

    result = map_description(prepared.svg, dto, producer="model-description:test")

    assert result.description is not None
    assert tuple(claim.text for claim in result.description.claims) == ("VONB",)
    assert result.description.verification is Verification.PENDING
    assert result.description.execution_mode is ExecutionMode.PRODUCTION
    assert result.diagnostics == ("claims[1]:missing_source_evidence",)
    assert result.description.claims[0].evidence.confidence == Confidence(
        None,
        "model-declared confidence label unsupported; uncalibrated and not verification",
    )


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "Infinity"])
def test_invalid_numeric_claim_is_dropped_without_guessing(value: str) -> None:
    prepared = _prepared()
    vonb = next(element for element in prepared.svg.elements if element.text == "VONB")
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=prepared.svg.digest,
        claims=(_claim(element_ids=(vonb.element_id,), value=value),),
        diagnostics=(),
    )

    result = map_description(prepared.svg, dto, producer="model-description:test")

    assert result.description is None
    assert result.diagnostics == ("claims[0]:invalid_decimal_observation",)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "high",
            Confidence(
                None,
                "model-declared ordinal=high; uncalibrated, not a probability or verification",
            ),
        ),
        (
            "0.75",
            Confidence(
                Decimal("0.75"),
                "model-self-assessment; not calibrated or independently verified",
            ),
        ),
        (
            "12",
            Confidence(
                None,
                "model-declared confidence label unsupported; uncalibrated and not verification",
            ),
        ),
        (
            "source_text_observation",
            Confidence(
                None,
                "model-declared confidence label unsupported; uncalibrated and not verification",
            ),
        ),
        (
            None,
            Confidence(None, "model confidence unavailable; uncalibrated and not verification"),
        ),
    ],
)
def test_model_confidence_is_preserved_only_when_bounded_and_never_verifies(
    label: str | None, expected: Confidence
) -> None:
    prepared = _prepared()
    vonb = next(element for element in prepared.svg.elements if element.text == "VONB")
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=prepared.svg.digest,
        claims=(_claim(element_ids=(vonb.element_id,), confidence=label),),
        diagnostics=(),
    )

    result = map_description(prepared.svg, dto, producer="model-description:test")

    assert result.description is not None
    assert result.description.claims[0].evidence.confidence == expected
    assert result.description.claims[0].evidence.verification is Verification.PENDING


def test_duplicate_evidence_is_not_silently_rewritten() -> None:
    prepared = _prepared()
    vonb = next(element for element in prepared.svg.elements if element.text == "VONB")
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=prepared.svg.digest,
        claims=(_claim(element_ids=(vonb.element_id, vonb.element_id)),),
        diagnostics=(),
    )

    result = map_description(prepared.svg, dto, producer="model-description:test")

    assert result.description is None
    assert result.diagnostics == ("claims[0]:duplicate_source_evidence",)


def test_actual_source_text_confidence_label_does_not_drop_explicit_value() -> None:
    prepared = _prepared()
    value = next(element for element in prepared.svg.elements if element.text == "72")
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=prepared.svg.digest,
        claims=(
            _claim(
                element_ids=(value.element_id,),
                confidence="source_text_observation",
                value="+72",
                text="VONB for Agency: +72 %.",
            ),
        ),
        diagnostics=(),
    )

    result = map_description(prepared.svg, dto, producer="model-description:test")

    assert result.description is not None
    assert result.description.claims[0].value == Decimal("+72")
    assert result.diagnostics == ()


def test_mapping_rejects_cross_svg_result_and_empty_producer() -> None:
    prepared = _prepared()
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest="0" * 64,
        claims=(),
        diagnostics=(),
    )
    with pytest.raises(ValueError, match="model_svg_binding_mismatch"):
        map_description(prepared.svg, dto, producer="model-description:test")
    with pytest.raises(ValueError, match="producer"):
        map_description(
            prepared.svg,
            dto.model_copy(update={"svg_digest": prepared.svg.digest}),
            producer="",
        )
