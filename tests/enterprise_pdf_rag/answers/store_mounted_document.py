"""Store-backed bridge satisfying ``MountedDocument`` for the answer-chain tests.

It composes the existing retrieval, chart and displayed-bar resolvers over one
pinned processing release, exactly as the catalog mount does, without depending
on the catalog module. Fixture builders publish an authored bar (1H21=15%,
1H22 unavailable, 1H23=6%) and the authored donut.
"""

from pathlib import Path

from enterprise_pdf_rag.adapters.chart_qa import StoredChartResolver
from enterprise_pdf_rag.adapters.chart_qa_bar_promotion import create_displayed_bar_draft
from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_retrieval import (
    CONTEXTUAL_POLICIES,
    ProcessingRetrieval,
    member_anchor,
    member_text,
    resolve_processing_context,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.ports import MemberText
from enterprise_pdf_rag.processing.index_text import PageIndexContext
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import DisplayedLookupContext
from ragspine.extraction.evidence.figures.chart_qa.models import ChartContext, QueryPin
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingManifest
from tests.enterprise_pdf_rag.adapters.chart_qa_bar_fixture import published_bar_input
from tests.enterprise_pdf_rag.adapters.test_chart_qa_store import published_chart
from tests.enterprise_pdf_rag.processing.test_persistent_retrieval import RecordingEmbedding


class StoreMountedDocument:
    """One processing release pinned by id; every read goes through the real stores."""

    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        *,
        processing_id: str,
        embedder: EmbeddingPort | None,
    ) -> None:
        manifest = outputs.load(processing_id)
        if manifest.retrieval is None:
            raise ValueError("The pinned processing release has no retrieval snapshot")
        plan, _ = outputs.load_retrieval(manifest.retrieval)
        fingerprints = {member.embedding_fingerprint for member in plan.members}
        if len(fingerprints) != 1:
            raise ValueError("Retrieval snapshot mixes embedding providers")
        self._sources = sources
        self._outputs = outputs
        self._processing_id = processing_id
        self._pinned = manifest
        self._publication = manifest.retrieval
        self._fingerprint = fingerprints.pop()
        self._retrieval = (
            None if embedder is None else ProcessingRetrieval(sources, outputs, embedder)
        )
        self._charts = StoredChartResolver(sources, outputs, processing_id=processing_id)
        self._displayed = StoredDisplayResolver(sources, outputs, processing_id=processing_id)

    @property
    def source_sha256(self) -> str:
        return self._pinned.scope.source_sha256

    @property
    def processing_id(self) -> str:
        return self._processing_id

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._publication.snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return self._fingerprint

    def manifest(self) -> ProcessingManifest:
        manifest = self._outputs.load(self._processing_id)
        if manifest != self._pinned:
            raise ValueError("Immutable processing manifest changed")
        return manifest

    def member_texts(self) -> tuple[MemberText, ...]:
        plan, _ = self._outputs.load_retrieval(self._publication)
        contexts = self._outputs.index_contexts(self._pinned)
        contextual = plan.qualification_policy in CONTEXTUAL_POLICIES
        texts = [
            MemberText(
                member.member_id,
                member.kind,
                member.page_index,
                member_text(self._outputs.assets, plan, member, contexts.get(member.page_index)),
                header=self._header(contexts.get(member.page_index)) if contextual else "",
                bbox=member_anchor(self._outputs.assets, member),
            )
            for member in plan.members
        ]
        return tuple(sorted(texts, key=lambda item: item.member_id))

    @staticmethod
    def _header(context: PageIndexContext | None) -> str:
        return "" if context is None else context.header()

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        if self._retrieval is None:
            raise RuntimeError("No query embedder was injected into this mount")
        return self._retrieval.search(self._publication, query, limit=limit)

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        return resolve_processing_context(self._sources, self._outputs, self._publication, hit)

    def _pin(self, hit: PinnedRetrievalHit) -> QueryPin:
        if hit.snapshot_id != self._publication.snapshot_id:
            raise ValueError("Retrieval hit belongs to another semantic snapshot")
        return QueryPin(self._processing_id, hit.snapshot_id, hit.member_id)

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        return self._charts.resolve(self._pin(hit))

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        return self._displayed.resolve(self._pin(hit))

    def member_ids_by_kind(self, kind: ObjectKind) -> tuple[str, ...]:
        """Test convenience: pinned member ids of one kind, ordered by member id."""
        return tuple(item.member_id for item in self.member_texts() if item.kind is kind)


def bar_document(tmp_path: Path) -> tuple[StoreMountedDocument, QueryPin]:
    """Publish the authored bar as a displayed-value member next to its literal footer."""
    sources, outputs, prior_id, item, _ = published_bar_input(tmp_path)
    release = create_displayed_bar_draft(
        sources,
        outputs,
        RecordingEmbedding(),
        processing_id=prior_id,
        page_index=0,
        object_id=item.object_id,
    )
    document = StoreMountedDocument(
        sources,
        ProcessingStore(outputs.root),
        processing_id=release.current.processing_id,
        embedder=RecordingEmbedding(),
    )
    return document, release.current


def donut_document(tmp_path: Path) -> tuple[StoreMountedDocument, QueryPin]:
    """Publish the authored donut (explicit distribution shares) as its only member."""
    sources, outputs, pin = published_chart(tmp_path)
    document = StoreMountedDocument(
        sources, outputs, processing_id=pin.processing_id, embedder=RecordingEmbedding()
    )
    return document, pin
