"""Versioned processing state and model-layout boundaries."""

from pydantic import Field

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.processing.models import ProcessingManifest, StageOutcome


class ProcessingEnvelope(BoundaryModel):
    manifest: ProcessingManifest


class ProcessingSnapshotResponse(BoundaryModel):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: ProcessingManifest


class StageEnvelope(BoundaryModel):
    outcome: StageOutcome


class ProcessingStatusResponse(BoundaryModel):
    processing_id: str
    source_manifest_id: str
    source_page_count: int
    selected_physical_pages: tuple[int, ...]
    object_count: int
    ir_artifacts: int
    description_artifacts: int
    failed_stages: int
    unavailable_stages: int
    deferred_stages: int
    qualified_claim_count: int
    retrieval_snapshot_id: str | None


class ProcessingSearchRequest(BoundaryModel):
    processing_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=100)
