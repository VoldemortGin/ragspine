"""Several mounted documents read as one retrieval corpus (ADR 0032).

``CrossDocument`` satisfies the ``MountedDocument`` port so the unchanged answer chain —
hybrid search, seat selection, the page window, claim verification — runs over every
mounted document at once. It owns no evidence: every member belongs to exactly one real
document, and every read (``resolve``, ``chart_context``, ``displayed_context``) is handed to
that document with its own pinned snapshot id, so a block, a verified claim and its citation
can only ever come from the document that holds the member.

The corpus is the union of the documents' members. Member ids are content addresses
(``RetrievalMember.member_id``), so two documents share an id only when they hold the
byte-identical member; the first document by sha256 keeps it and the other copy is left out,
which keeps the union's ids unique without renaming anything a claim or citation names.

The lexical channel scores the union as one BM25 corpus (one set of corpus statistics, so
scores compare across documents). The vector channel asks each document for its own top
``limit`` and keeps the best ``limit`` of the merge — cosine scores from one embedder compare
directly — so a channel never offers more candidates than it does for one document.
"""

from collections.abc import Sequence
from hashlib import sha256

from enterprise_pdf_rag.answers.models import FusedHit
from enterprise_pdf_rag.answers.ports import MemberText, MountedDocument
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import DisplayedLookupContext
from ragspine.extraction.evidence.figures.chart_qa.models import ChartContext
from ragspine.extraction.evidence.page.models import ProcessingManifest


class CrossDocument:
    """A read-only union of mounted documents; each member is read from its own document."""

    def __init__(self, documents: Sequence[MountedDocument]) -> None:
        if len(documents) < 2:
            raise ValueError("A cross-document corpus needs at least two documents")
        self._documents = tuple(sorted(documents, key=lambda item: item.source_sha256))
        if len({item.source_sha256 for item in self._documents}) != len(self._documents):
            raise ValueError("A cross-document corpus names each document once")
        self._snapshot_id = sha256(
            repr(
                ("cross-document-v1", tuple(item.retrieval_snapshot_id for item in self._documents))
            ).encode()
        ).hexdigest()
        self._texts: tuple[MemberText, ...] | None = None
        self._owners: dict[str, MountedDocument] = {}

    @property
    def documents(self) -> tuple[MountedDocument, ...]:
        """The member documents, ordered by source sha256."""
        return self._documents

    @property
    def document_ids(self) -> tuple[str, ...]:
        return tuple(item.source_sha256 for item in self._documents)

    @property
    def source_sha256(self) -> str:
        # Not a document: the corpus identity. A result always names a real document instead.
        return self._snapshot_id

    @property
    def processing_id(self) -> str:
        return self._snapshot_id

    @property
    def retrieval_snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def embedding_fingerprint(self) -> str:
        return "+".join(sorted({item.embedding_fingerprint for item in self._documents}))

    def manifest(self) -> ProcessingManifest:
        raise ValueError("A cross-document corpus has no manifest of its own; read a document's")

    def member_texts(self) -> tuple[MemberText, ...]:
        """Every member of every document once, ordered by member id; no model call."""
        if self._texts is None:
            texts: list[MemberText] = []
            for document in self._documents:
                for member in document.member_texts():
                    if member.member_id in self._owners:
                        continue
                    self._owners[member.member_id] = document
                    texts.append(member)
            self._texts = tuple(sorted(texts, key=lambda item: item.member_id))
        return self._texts

    def owner(self, member_id: str) -> MountedDocument:
        """The document a member is read from."""
        if self._texts is None:
            self.member_texts()
        document = self._owners.get(member_id)
        if document is None:
            raise ValueError("Retrieval hit is not a member of any mounted document")
        return document

    def members_of(self, document: MountedDocument) -> frozenset[str]:
        """The member ids this corpus reads from ``document``."""
        self.member_texts()
        return frozenset(
            member_id for member_id, owner in self._owners.items() if owner is document
        )

    def search(self, query: str, *, limit: int) -> tuple[PinnedRetrievalHit, ...]:
        """Each document's own top ``limit``, merged by score and cut to ``limit``."""
        merged = [
            (hit.score, hit.member_id)
            for document in self._documents
            for hit in document.search(query, limit=limit)
            if self.owner(hit.member_id) is document
        ]
        merged.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            PinnedRetrievalHit(self._snapshot_id, member_id, score)
            for score, member_id in merged[:limit]
        )

    def _pinned(self, hit: PinnedRetrievalHit) -> tuple[MountedDocument, PinnedRetrievalHit]:
        document = self.owner(hit.member_id)
        if hit.snapshot_id not in (self._snapshot_id, document.retrieval_snapshot_id):
            raise ValueError("Retrieval hit belongs to another semantic snapshot")
        return document, PinnedRetrievalHit(
            document.retrieval_snapshot_id, hit.member_id, hit.score
        )

    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        document, pinned = self._pinned(hit)
        return document.resolve(pinned)

    def chart_context(self, hit: PinnedRetrievalHit) -> ChartContext:
        document, pinned = self._pinned(hit)
        return document.chart_context(pinned)

    def displayed_context(self, hit: PinnedRetrievalHit) -> DisplayedLookupContext:
        document, pinned = self._pinned(hit)
        return document.displayed_context(pinned)

    def repin(self, hits: Sequence[FusedHit]) -> tuple[FusedHit, ...]:
        """The fused ranking pinned back to each member's own document and snapshot."""
        repinned: list[FusedHit] = []
        for hit in hits:
            document = self.owner(hit.member_id)
            repinned.append(
                FusedHit(
                    document.retrieval_snapshot_id,
                    hit.member_id,
                    hit.fused_score,
                    hit.vector_rank,
                    hit.lexical_rank,
                    hit.vector_score,
                    hit.bm25_score,
                    hit.tree_rank,
                    document.source_sha256,
                )
            )
        return tuple(repinned)
