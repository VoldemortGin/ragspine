"""Versioned duplicate-occurrence normalization of an independent raw branch."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict

from enterprise_pdf_rag.adapters.chart_semantic_schemas import FigureDescriptionDTO
from enterprise_pdf_rag.adapters.description_mapping import map_description
from ragspine.extraction.evidence.figures.models import SvgArtifact, SvgBinding, TextDescription

NORMALIZATION_VERSION = "duplicate-evidence-normalization-v1"


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class EvidenceIdNormalization(_Strict):
    claim_index: int
    original_ids: tuple[str, ...]
    normalized_ids: tuple[str, ...]
    removed_positions: tuple[int, ...]


class DescriptionNormalizationReceipt(_Strict):
    schema_version: Literal["description-evidence-normalization-v1"] = (
        "description-evidence-normalization-v1"
    )
    producer: Literal["duplicate-evidence-normalization-v1"] = "duplicate-evidence-normalization-v1"
    binding: SvgBinding
    raw_sha256: str
    original_typed_artifact_id: str
    normalized_dto_sha256: str
    output_description_artifact_id: str
    changes: tuple[EvidenceIdNormalization, ...]

    @property
    def artifact_id(self) -> str:
        return "description-normalization-v1:" + sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedDescription:
    description: TextDescription
    normalized: FigureDescriptionDTO
    receipt: DescriptionNormalizationReceipt


def normalize_description_evidence(
    *,
    svg: SvgArtifact,
    raw_json: bytes,
    previous: TextDescription,
) -> NormalizedDescription:
    """Keep all raw semantics and record only intra-claim occurrence de-duplication."""
    original = FigureDescriptionDTO.model_validate_json(raw_json)
    original_mapping = map_description(svg, original, producer=previous.producer)
    if original_mapping.description != previous:
        raise ValueError("Normalization lineage differs from the original typed description")
    changes = tuple(
        EvidenceIdNormalization(
            claim_index=index,
            original_ids=claim.evidence.element_ids,
            normalized_ids=tuple(dict.fromkeys(claim.evidence.element_ids)),
            removed_positions=tuple(
                position
                for position, item in enumerate(claim.evidence.element_ids)
                if item in claim.evidence.element_ids[:position]
            ),
        )
        for index, claim in enumerate(original.claims)
        if len(set(claim.evidence.element_ids)) != len(claim.evidence.element_ids)
    )
    normalized = original.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(
                    update={
                        "evidence": claim.evidence.model_copy(
                            update={
                                "element_ids": tuple(dict.fromkeys(claim.evidence.element_ids)),
                            }
                        )
                    }
                )
                for claim in original.claims
            )
        }
    )
    mapped = map_description(
        svg, normalized, producer=f"{NORMALIZATION_VERSION}:{previous.producer}"
    )
    if mapped.description is None or mapped.diagnostics:
        raise ValueError("Description normalization must preserve every original claim")
    receipt = DescriptionNormalizationReceipt(
        binding=svg.binding,
        raw_sha256=sha256(raw_json).hexdigest(),
        original_typed_artifact_id=previous.artifact_id,
        normalized_dto_sha256=sha256(normalized.model_dump_json().encode()).hexdigest(),
        output_description_artifact_id=mapped.description.artifact_id,
        changes=changes,
    )
    return NormalizedDescription(mapped.description, normalized, receipt)
