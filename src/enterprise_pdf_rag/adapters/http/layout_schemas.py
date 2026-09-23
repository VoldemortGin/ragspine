"""Strict model output for page partitioning; all fields are explicitly required."""

from pydantic import Field

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from ragspine.extraction.evidence.page.models import ObjectKind


class LayoutRegionDTO(BoundaryModel):
    region_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    kind: ObjectKind
    bbox: tuple[float, float, float, float]
    source_span_ids: tuple[str, ...]
    context_span_ids: tuple[str, ...]
    list_items: tuple[tuple[str, ...], ...]
    list_ordered: bool | None
    parent_id: str | None
    interpretation: str


class PageLayoutDTO(BoundaryModel):
    regions: tuple[LayoutRegionDTO, ...]
    unassigned_span_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
