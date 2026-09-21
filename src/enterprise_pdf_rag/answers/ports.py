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
    """The embedded index text of one pinned member; the lexical corpus unit."""

    member_id: str
    kind: ObjectKind
    page_index: int
    text: str
    # Verified page metadata (ADR 0013); absent on snapshots without the stage.
    page_title: str | None = None
    section: str | None = None
    page_type: str | None = None
    periods: tuple[str, ...] = ()  # canonical forms only (``1H2026``)
    regions: tuple[str, ...] = ()  # verbatim page values
    # The subset of ``regions`` the page geometry binds to this member alone, for a page
    # that prints several charts side by side under their own headings. Empty means the
    # page could not be read as columns, and the page-level values stand.
    member_regions: tuple[str, ...] = ()
    # The ADR 0013 contextual header prefixed to ``text``; ``body`` is what follows it.
    header: str = ""
    # The member's page rectangle, for reading order within a page; absent on snapshots
    # whose stored evidence carries no anchor for this kind.
    bbox: tuple[float, float, float, float] | None = None

    @property
    def body(self) -> str:
        """The index text without its contextual header (ADR 0013)."""
        prefix = self.header + "\n"
        if self.header and self.text.startswith(prefix):
            return self.text[len(prefix) :]
        return self.text


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
        """Every member's embedded index text, ordered by member id; no model call."""
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
