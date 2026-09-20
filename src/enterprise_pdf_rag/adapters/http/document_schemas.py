"""Strict wire contracts for source review, separate from qualified figure QA."""

from pydantic import Field

from enterprise_pdf_rag.adapters.http.openai_schemas import ChatMessage, StreamOptions
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.documents.models import DocumentManifest, TextSidecar


class ManifestEnvelope(BoundaryModel):
    manifest: DocumentManifest


class DocumentSnapshotResponse(BoundaryModel):
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: DocumentManifest


class PageTextResponse(BoundaryModel):
    sidecar: TextSidecar


class SourceChatRequest(BoundaryModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    stream: bool = False
    stream_options: StreamOptions | None = None
    snapshot_id: str | None = None
