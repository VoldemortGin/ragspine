"""Infrastructure contracts for source extraction and content-addressed persistence."""

from typing import Protocol, runtime_checkable

from enterprise_pdf_rag.documents.models import (
    AssetRef,
    Bounds,
    DocumentExtraction,
    DocumentManifest,
    RegionExtraction,
)


@runtime_checkable
class SourceExtractor(Protocol):
    def extract_document(self, pdf: bytes) -> DocumentExtraction: ...

    def extract_region(self, pdf: bytes, *, page_index: int, bbox: Bounds) -> RegionExtraction: ...


@runtime_checkable
class AssetStore(Protocol):
    def put(self, data: bytes, *, media_type: str) -> AssetRef: ...

    def get(self, ref: AssetRef) -> bytes: ...

    def publish(self, manifest: DocumentManifest) -> str: ...
