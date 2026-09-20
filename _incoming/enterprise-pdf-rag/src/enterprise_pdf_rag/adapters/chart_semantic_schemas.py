"""Strict model-boundary observations; qualification is never a model field."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class EvidenceDTO(_StrictModel):
    element_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    confidence: str | None


class TextFieldDTO(_StrictModel):
    text: str = Field(min_length=1, max_length=300)
    evidence: EvidenceDTO


class NumericDTO(_StrictModel):
    value: str | None
    kind: Literal["explicit", "derived", "estimated", "unavailable"]
    evidence: EvidenceDTO


class ChartPointDTO(_StrictModel):
    point_id: str = Field(min_length=1, max_length=80)
    series: TextFieldDTO
    category: TextFieldDTO
    unit: TextFieldDTO
    value: NumericDTO


class AxisDTO(_StrictModel):
    axis_id: str
    label: TextFieldDTO
    unit: TextFieldDTO
    scale: Literal["linear", "logarithmic", "categorical", "unknown"]


class MarkDTO(_StrictModel):
    mark_id: str
    kind: Literal["arc", "bar", "line", "point", "unknown"]
    bbox: tuple[float, float, float, float]
    color: str | None
    point_ids: tuple[str, ...]
    evidence: EvidenceDTO


class ChartObservationsDTO(_StrictModel):
    schema_version: Literal["chart-observations-v1"]
    svg_digest: str
    grammar: Literal["donut", "pie", "bar", "line", "waterfall", "unknown"]
    title: TextFieldDTO | None
    period: TextFieldDTO | None
    axes: tuple[AxisDTO, ...] = Field(max_length=4)
    points: tuple[ChartPointDTO, ...] = Field(max_length=40)
    marks: tuple[MarkDTO, ...] = Field(max_length=40)
    diagnostics: tuple[str, ...]


class DescriptionClaimDTO(_StrictModel):
    text: str = Field(min_length=1, max_length=1200)
    evidence: EvidenceDTO
    series: str | None
    category: str | None
    unit: str | None
    value: str | None
    period: str | None


class FigureDescriptionDTO(_StrictModel):
    schema_version: Literal["figure-description-v1"]
    svg_digest: str
    claims: tuple[DescriptionClaimDTO, ...] = Field(max_length=40)
    diagnostics: tuple[str, ...]
