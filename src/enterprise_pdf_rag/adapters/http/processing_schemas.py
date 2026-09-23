"""Versioned processing state and model-layout boundaries."""

from typing import Literal

from pydantic import Field, model_validator

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import ProcessingManifest, StageOutcome, StageState


class ProcessingEnvelope(BoundaryModel):
    manifest: ProcessingManifest


class ProcessingSnapshotResponse(BoundaryModel):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: ProcessingManifest


class StageEnvelope(BoundaryModel):
    outcome: StageOutcome


class DocumentTreeRecord(BoundaryModel):
    """The saved state of one processing id's routing tree (ADR 0019); a later run replaces it."""

    schema_version: Literal["document-tree-v1"] = "document-tree-v1"
    processing_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer: str = Field(min_length=1)
    state: StageState
    diagnostic: str | None
    artifact: AssetRef | None
    summary_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def _succeeded_records_name_their_tree(self) -> "DocumentTreeRecord":
        if self.state is StageState.SUCCEEDED and (self.artifact is None or self.diagnostic):
            raise ValueError("A succeeded document tree names its artifact and no diagnostic")
        if self.state is not StageState.SUCCEEDED and not (self.diagnostic or "").strip():
            raise ValueError("An unfinished document tree requires a concrete diagnostic")
        return self


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
