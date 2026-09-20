"""Read-only document seam consumed by hybrid search and the answer service.

The real implementation lives in ``adapters/document_catalog.py``; tests use a
store-backed bridge. Nothing here performs I/O or calls a model.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from enterprise_pdf_rag.figures.chart_qa.displayed_models import DisplayedLookupContext
from enterprise_pdf_rag.figures.chart_qa.models import ChartContext
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingManifest
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext


@dataclass(frozen=True, slots=True)
class MemberText:
    """The embedded description text of one pinned member; the lexical corpus unit."""

    member_id: str
    kind: ObjectKind
    page_index: int
    text: str


@runtime_checkable
class MountedDocument(Protocol):
    """One published document pinned to a single retrieval snapshot."""

    @property
    def source_sha256(self) -> str: ...

    @property
    def processing_id(self) -> str: ...

    @property
    def retrieval_snapshot_id(self) -> str: ...

    @property
    def embedding_fingerprint(self) -> str: ...

    def manifest(self) -> ProcessingManifest:
        """Reload the pinned processing manifest and refuse any drift."""
        ...

    def member_texts(self) -> tuple[MemberText, ...]:
        """Every member's embedded description text, ordered by member id; no model call."""
        ...

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        """Vector channel over the pinned index using the explicitly injected embedder."""
        ...

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        """Hydrate and re-verify stored evidence for one hit; never calls a model."""
        ...

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        """Requalify a chart member with its SVG evidence; non-chart members are refused."""
        ...

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        """Requalify a displayed-value bar member; any other member is refused."""
        ...
