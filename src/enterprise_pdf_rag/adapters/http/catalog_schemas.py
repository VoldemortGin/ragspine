"""Public ``document-catalog-v1`` contract: listing, pinned status, per-document search and context."""

from typing import Literal

from pydantic import Field

from enterprise_pdf_rag.adapters.document_catalog import CatalogEntry
from enterprise_pdf_rag.adapters.http.processing_review import RetrievalHitInput
from enterprise_pdf_rag.adapters.http.processing_schemas import ProcessingStatusResponse
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext


class DocumentListItem(CatalogEntry):
    """A catalog entry plus whether this process holds a pinned mount for it."""

    mounted: bool
    mount_error: str | None = None

    @classmethod
    def from_entry(
        cls, entry: CatalogEntry, *, mounted: bool, mount_error: str | None
    ) -> "DocumentListItem":
        return cls(**entry.model_dump(), mounted=mounted, mount_error=mount_error)


class DocumentListResponse(BoundaryModel):
    schema_version: Literal["document-catalog-v1"] = "document-catalog-v1"
    ingestion_root: str
    legacy_roots: tuple[str, ...]
    embedding_configured: bool
    embedding_fingerprint: str | None
    documents: tuple[DocumentListItem, ...]
    unpublished: tuple[str, ...]


class DocumentDetailResponse(BoundaryModel):
    schema_version: Literal["document-catalog-v1"] = "document-catalog-v1"
    document: DocumentListItem
    status: ProcessingStatusResponse | None


class DocumentSearchRequest(BoundaryModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=100)


class DocumentSearchResponse(BoundaryModel):
    document_id: str
    processing_id: str
    snapshot_id: str
    hits: tuple[PinnedRetrievalHit, ...]


class DocumentContextRequest(BoundaryModel):
    hit: RetrievalHitInput


class DocumentContextResponse(BoundaryModel):
    document_id: str
    processing_id: str
    context: RetrievalContext
