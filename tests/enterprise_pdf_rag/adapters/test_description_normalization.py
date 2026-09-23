"""Duplicate-ID repair preserves the independent branch, never invents claims."""

from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    DescriptionClaimDTO,
    EvidenceDTO,
    FigureDescriptionDTO,
)
from enterprise_pdf_rag.adapters.description_mapping import map_description
from enterprise_pdf_rag.adapters.description_normalization import (
    normalize_description_evidence,
)
from ragspine.extraction.evidence.figures.models import SvgArtifact, TextDescription, Verification
from tests.enterprise_pdf_rag.figures.test_chart_qa_displayed import displayed_context


def independent_branch() -> tuple[SvgArtifact, bytes, TextDescription]:
    context = displayed_context()
    claims = tuple(
        DescriptionClaimDTO(
            text=claim.text,
            evidence=EvidenceDTO(
                element_ids=(claim.evidence.element_ids[0], *claim.evidence.element_ids)
                if i == 0
                else claim.evidence.element_ids,
                confidence="high",
            ),
            series=claim.series,
            category=claim.category,
            unit=claim.unit,
            value=str(claim.value),
            period=claim.period,
        )
        for i, claim in enumerate(context.description.claims)
    )
    dto = FigureDescriptionDTO(
        schema_version="figure-description-v1",
        svg_digest=context.svg.digest,
        claims=claims,
        diagnostics=("raw provider diagnostic stays unchanged",),
    )
    previous = map_description(context.svg, dto, producer="original-independent-model").description
    assert previous is not None and len(previous.claims) == 1
    return context.svg, dto.model_dump_json().encode(), previous


def test_exact_duplicate_repair_restores_raw_claim_without_changing_its_semantics() -> None:
    svg, raw_json, previous = independent_branch()
    old_id = previous.artifact_id
    result = normalize_description_evidence(svg=svg, raw_json=raw_json, previous=previous)
    assert len(previous.claims) == 1 and previous.artifact_id == old_id
    assert len(result.description.claims) == 2
    repaired = result.description.claims[0]
    assert (
        repaired.text,
        repaired.series,
        repaired.category,
        repaired.period,
        repaired.unit,
        repaired.value,
    ) == (
        "Expense Ratio for 1H24: 8.2 %.",
        "Expense Ratio",
        "1H24",
        None,
        "%",
        Decimal("8.2"),
    )
    assert repaired.evidence.confidence == previous.claims[0].evidence.confidence
    assert result.description.verification is Verification.PENDING
    assert all(
        claim.evidence.verification is Verification.PENDING for claim in result.description.claims
    )
    assert result.description.artifact_id != old_id
    original = FigureDescriptionDTO.model_validate_json(raw_json)
    assert result.normalized.claims[0].model_dump(exclude={"evidence"}) == original.claims[
        0
    ].model_dump(exclude={"evidence"})
    assert result.normalized.claims[0].evidence.confidence == original.claims[0].evidence.confidence
    assert result.normalized.claims[1] == original.claims[1]
    assert result.normalized.diagnostics == original.diagnostics
    assert result.receipt.raw_sha256
    assert result.receipt.original_typed_artifact_id == old_id
    assert result.receipt.output_description_artifact_id == result.description.artifact_id
    assert result.receipt.changes[0].claim_index == 0
    assert result.receipt.changes[0].removed_positions == (1,)
    assert result.receipt.changes[0].normalized_ids == tuple(
        dict.fromkeys(original.claims[0].evidence.element_ids)
    )
    again = normalize_description_evidence(svg=svg, raw_json=raw_json, previous=previous)
    assert again == result


def test_an_unrelated_previous_typed_artifact_cannot_be_used_as_repair_lineage() -> None:
    svg, raw_json, previous = independent_branch()
    wrong = replace(
        previous, claims=(replace(previous.claims[0], text="unrelated model sentence"),)
    )
    with pytest.raises(ValueError, match="original typed"):
        normalize_description_evidence(svg=svg, raw_json=raw_json, previous=wrong)


@pytest.mark.parametrize(
    "fault", ("unknown_id", "empty_id", "conflicting_pool_id", "different_source")
)
def test_normalization_cannot_hide_ambiguous_or_missing_source_evidence(
    fault: str,
) -> None:
    svg, raw_json, previous = independent_branch()
    dto = FigureDescriptionDTO.model_validate_json(raw_json)
    if fault in {"unknown_id", "empty_id"}:
        first = dto.claims[0]
        bad_id = "missing-source" if fault == "unknown_id" else ""
        first = first.model_copy(
            update={
                "evidence": first.evidence.model_copy(
                    update={
                        "element_ids": (*first.evidence.element_ids, bad_id, bad_id),
                    }
                )
            }
        )
        dto = dto.model_copy(update={"claims": (first, *dto.claims[1:])})
        raw_json = dto.model_dump_json().encode()
    elif fault == "conflicting_pool_id":
        conflict = replace(svg.elements[0], text="Other sources")
        svg = replace(svg, elements=(*svg.elements, conflict))
    else:
        wrong_source = replace(
            svg.elements[0], anchor=replace(svg.elements[0].anchor, page_index=18)
        )
        svg = replace(svg, elements=(wrong_source, *svg.elements[1:]))
    with pytest.raises(ValueError):
        normalize_description_evidence(svg=svg, raw_json=raw_json, previous=previous)


def test_decimal_precision_and_confidence_are_preserved_not_corrected() -> None:
    svg, raw_json, previous = independent_branch()
    dto = FigureDescriptionDTO.model_validate_json(raw_json)
    first = dto.claims[0].model_copy(
        update={
            "value": "8.20",
            "evidence": dto.claims[0].evidence.model_copy(update={"confidence": "0.25"}),
        }
    )
    dto = dto.model_copy(update={"claims": (first, *dto.claims[1:])})
    result = normalize_description_evidence(
        svg=svg, raw_json=dto.model_dump_json().encode(), previous=previous
    )
    assert result.normalized.claims[0].value == "8.20"
    assert result.normalized.claims[0].text == first.text
    assert str(result.description.claims[0].value) == "8.20"
    assert result.description.claims[0].evidence.confidence.score == Decimal("0.25")
    assert result.description.claims[0].evidence.verification is Verification.PENDING
    assert result.description.claims[1] == previous.claims[0]
