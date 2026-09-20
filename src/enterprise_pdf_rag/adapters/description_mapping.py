"""Map independent description DTO claims without guessing failed evidence."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    DescriptionClaimDTO,
    FigureDescriptionDTO,
)
from enterprise_pdf_rag.figures.models import (
    Confidence,
    DescriptionClaim,
    Evidence,
    ExecutionMode,
    SvgArtifact,
    TextDescription,
    Verification,
)
from enterprise_pdf_rag.figures.validation import validate_svg


@dataclass(frozen=True, slots=True)
class DescriptionMapping:
    description: TextDescription | None
    diagnostics: tuple[str, ...]


def _confidence(value: str | None) -> Confidence:
    if value is None:
        return Confidence(None, "model confidence unavailable; uncalibrated and not verification")
    if value in {"high", "medium", "low", "unknown"}:
        return Confidence(
            None,
            f"model-declared ordinal={value}; uncalibrated, not a probability or verification",
        )
    try:
        score = Decimal(value)
    except InvalidOperation:
        score = None
    if score is not None and score.is_finite() and Decimal(0) <= score <= Decimal(1):
        return Confidence(score, "model-self-assessment; not calibrated or independently verified")
    return Confidence(
        None,
        "model-declared confidence label unsupported; uncalibrated and not verification",
    )


def _claim(svg: SvgArtifact, value: DescriptionClaimDTO) -> DescriptionClaim:
    if len(set(value.evidence.element_ids)) != len(value.evidence.element_ids):
        raise ValueError("duplicate_source_evidence")
    available = {element.element_id for element in svg.elements}
    if any(element_id not in available for element_id in value.evidence.element_ids):
        raise ValueError("missing_source_evidence")
    numeric: Decimal | None = None
    if value.value is not None:
        try:
            numeric = Decimal(value.value)
        except InvalidOperation:
            raise ValueError("invalid_decimal_observation") from None
        if not numeric.is_finite():
            raise ValueError("invalid_decimal_observation")
    evidence = Evidence(
        value.evidence.element_ids,
        Verification.PENDING,
        _confidence(value.evidence.confidence),
    )
    return DescriptionClaim(
        value.text,
        evidence,
        value.series,
        value.category,
        value.unit,
        numeric,
        value.period,
    )


def map_description(
    svg: SvgArtifact, dto: FigureDescriptionDTO, *, producer: str
) -> DescriptionMapping:
    """Keep independently source-bound claims; locate and omit unsafe slices."""
    if not producer.strip():
        raise ValueError("Description producer is required")
    if dto.svg_digest != svg.digest:
        raise ValueError("model_svg_binding_mismatch")
    validate_svg(svg)
    claims: list[DescriptionClaim] = []
    diagnostics: list[str] = []
    for index, value in enumerate(dto.claims):
        try:
            claims.append(_claim(svg, value))
        except ValueError as error:
            diagnostics.append(f"claims[{index}]:{error}")
    description = (
        TextDescription(
            svg.binding,
            tuple(claims),
            producer,
            Verification.PENDING,
            ExecutionMode.PRODUCTION,
        )
        if claims
        else None
    )
    if not dto.claims:
        diagnostics.append("description_unavailable")
    return DescriptionMapping(description, tuple(diagnostics))
