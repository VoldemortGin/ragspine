"""Versioned boundary contracts mapped onto immutable application values."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from enterprise_pdf_rag.figures.models import (
    FigureBundle,
    FigureHit,
    ReasoningView,
    SvgArtifact,
)


class BoundaryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, str_strip_whitespace=True
    )


class DemoRequest(BoundaryModel):
    snapshot_id: str = Field(min_length=1, max_length=128)
    query: str = Field(
        default="Revenue 2025", min_length=1, max_length=2000, pattern=r"\w"
    )


class SearchRequest(BoundaryModel):
    snapshot_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000, pattern=r"\w")
    limit: int = Field(default=5, ge=1, le=100)


class HitSchema(BoundaryModel):
    snapshot_id: str
    bundle_id: str
    description_id: str
    figure_id: str
    source_revision: str
    svg_artifact_id: str
    svg_digest: str
    chart_ir_artifact_id: str
    text: str
    score: float = Field(default=0.0, allow_inf_nan=False)

    @classmethod
    def from_domain(cls, hit: FigureHit) -> "HitSchema":
        return cls(
            snapshot_id=hit.snapshot_id,
            bundle_id=hit.bundle_id,
            description_id=hit.description_id,
            figure_id=hit.figure_id,
            source_revision=hit.source_revision,
            svg_artifact_id=hit.svg_artifact_id,
            svg_digest=hit.svg_digest,
            chart_ir_artifact_id=hit.chart_ir_artifact_id,
            text=hit.text,
            score=hit.score,
        )

    def to_domain(self) -> FigureHit:
        return FigureHit(
            self.snapshot_id,
            self.bundle_id,
            self.description_id,
            self.figure_id,
            self.source_revision,
            self.svg_artifact_id,
            self.svg_digest,
            self.chart_ir_artifact_id,
            self.text,
            self.score,
        )


class ContextRequest(BoundaryModel):
    snapshot_id: str = Field(min_length=1, max_length=128)
    hit: HitSchema


class ContextResponse(BoundaryModel):
    schema_version: Literal["figure-api-v1"] = "figure-api-v1"
    execution_mode: Literal["offline-demo"] = "offline-demo"
    context: ReasoningView


class SearchResponse(BoundaryModel):
    schema_version: Literal["figure-api-v1"] = "figure-api-v1"
    execution_mode: Literal["offline-demo"] = "offline-demo"
    snapshot_id: str
    hits: tuple[HitSchema, ...]


class DemoResponse(BoundaryModel):
    schema_version: Literal["figure-api-v1"] = "figure-api-v1"
    execution_mode: Literal["offline-demo"] = "offline-demo"
    bundle: FigureBundle
    hits: tuple[HitSchema, ...]
    context: ReasoningView


class ExtractionResponse(BoundaryModel):
    schema_version: Literal["figure-api-v1"] = "figure-api-v1"
    artifact_id: str
    artifact: SvgArtifact
