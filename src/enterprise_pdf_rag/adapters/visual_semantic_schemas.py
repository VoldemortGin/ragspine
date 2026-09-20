"""Strict model boundaries for non-chart visual objects."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class VisualEvidenceDTO(_StrictModel):
    element_ids: tuple[str, ...] = Field(max_length=30)
    confidence: str | None = Field(max_length=80)


class ImageObjectDTO(_StrictModel):
    text: str = Field(min_length=1, max_length=500)
    evidence: VisualEvidenceDTO


class ImageObservationsDTO(_StrictModel):
    schema_version: Literal["image-observations-v1"]
    svg_digest: str
    visible_objects: tuple[ImageObjectDTO, ...] = Field(max_length=30)
    observed_label_element_ids: tuple[str, ...] = Field(max_length=30)
    confidence: str | None = Field(max_length=80)
    diagnostics: tuple[str, ...]


class DiagramNodeDTO(_StrictModel):
    node_id: str = Field(min_length=1, max_length=80)
    label: str | None = Field(max_length=300)
    bbox: tuple[float, float, float, float]
    evidence: VisualEvidenceDTO


class DiagramEdgeDTO(_StrictModel):
    source_node_id: str
    target_node_id: str
    label: str | None = Field(max_length=300)
    relationship: str = Field(min_length=1, max_length=300)
    evidence: VisualEvidenceDTO


class DiagramObservationsDTO(_StrictModel):
    schema_version: Literal["diagram-observations-v1"]
    svg_digest: str
    nodes: tuple[DiagramNodeDTO, ...] = Field(max_length=50)
    edges: tuple[DiagramEdgeDTO, ...] = Field(max_length=100)
    confidence: str | None = Field(max_length=80)
    diagnostics: tuple[str, ...]


class FormulaObservationsDTO(_StrictModel):
    schema_version: Literal["formula-observations-v1"]
    svg_digest: str
    source_literal_element_ids: tuple[str, ...] = Field(max_length=50)
    normalization_state: Literal["inferred", "unavailable"]
    latex: str | None = Field(max_length=2000)
    confidence: str | None = Field(max_length=80)
    diagnostics: tuple[str, ...]


class VisualDescriptionDTO(_StrictModel):
    schema_version: Literal["visual-description-v1"]
    svg_digest: str
    text: str = Field(min_length=1, max_length=2000)
    evidence: VisualEvidenceDTO
    diagnostics: tuple[str, ...]
