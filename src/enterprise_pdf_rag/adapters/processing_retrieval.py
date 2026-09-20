"""Persist real description vectors and hydrate source-qualified typed artifacts."""

from collections.abc import Mapping
from hashlib import sha256
from math import sqrt

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.chart_member_validation import (
    uses_displayed_bar_policy,
    validate_retrieval_chart_member,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.literal_qualification import validate_literal_member
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    FigureQualification,
    TextDescription,
)
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from enterprise_pdf_rag.processing.index_text import (
    PageIndexContext,
    contextual_index_text,
    member_index_text,
)
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingScope,
    RetrievalPublication,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import (
    IndexEntry,
    PinnedRetrievalHit,
    RetrievalContext,
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalMember,
    RetrievalPlan,
    resolve_member,
    retrieval_dependencies,
)
from enterprise_pdf_rag.processing.table_models import TableIR
from enterprise_pdf_rag.processing.typed_ir import (
    GroupIR,
    ListIR,
    LiteralQualification,
    ObjectDescription,
    TextIR,
)

# v2 admits Table members whose literal transcription qualified; v3 embeds the chart
# index-text projection (ADR 0012) instead of the description alone; v4 prepends the
# page's contextual header (``display_title | page_title | section``, ADR 0013). The
# string is part of the snapshot id, so older snapshots keep their ids and stay mountable.
_POLICY = "source-transcription-and-scoped-chart-qualification-v4"
_INDEX = "immutable-cosine-index-v1"
# Snapshots whose vectors embed ``member_index_text``; older ones embedded the description
# text, and their lexical corpus must keep scoring exactly what they embedded. The
# displayed-bar admission builds its member through this class, so its v2 policy belongs
# here too (``chart_qa_bar_promotion.BAR_PUBLICATION_POLICY``).
PROJECTED_CHART_POLICIES = frozenset(
    {
        _POLICY,
        "source-transcription-and-scoped-chart-qualification-v3",
        "source-transcription-donut-and-displayed-bar-v2",
    }
)
# Snapshots whose vectors embed the contextual header above the projection.
CONTEXTUAL_POLICIES = frozenset({_POLICY})


def member_text(
    assets: LocalDocumentStore,
    plan: RetrievalPlan,
    member: RetrievalMember,
    context: PageIndexContext | None = None,
) -> str:
    """The text one pinned member was (or would be) embedded with; no evidence validation.

    ``context`` is the member's page header; it is applied only under a contextual policy,
    so a snapshot indexed before ADR 0013 keeps scoring exactly what it embedded.
    """
    payload = assets.get(member.description)
    if member.kind is not ObjectKind.CHART:
        body = TypeAdapter(ObjectDescription).validate_json(payload).text
    else:
        body = TypeAdapter(TextDescription).validate_json(payload).text
        if plan.qualification_policy in PROJECTED_CHART_POLICIES:
            chart = TypeAdapter(ChartIR).validate_json(assets.get(member.ir))
            body = member_index_text(chart, body)
    if plan.qualification_policy not in CONTEXTUAL_POLICIES:
        return body
    return contextual_index_text(body, context)


def eligibility(record: ObjectProcessingRecord) -> tuple[bool, str | None]:
    """Kind and stage-completeness predicate shared by build and draft qualification.

    A Table is verified only when its literal transcription qualification succeeded;
    an observed grid whose description/qualification stayed unavailable is skipped.
    """
    if record.kind not in (
        ObjectKind.TEXT,
        ObjectKind.LIST,
        ObjectKind.GROUP,
        ObjectKind.TABLE,
        ObjectKind.CHART,
    ):
        return False, f"{record.kind.value} objects are not retrievable"
    stages = {stage.stage: stage for stage in record.stages}
    required = (
        ("qualified_ir", "qualified_description", "qualification", "svg")
        if record.kind is ObjectKind.CHART
        else ("ir", "description", "qualification", "svg")
    )
    if any(
        name not in stages or stages[name].state is not StageState.SUCCEEDED for name in required
    ):
        if record.kind is ObjectKind.TABLE:
            return (
                False,
                "Table transcription is not verified; only verified tables are retrievable",
            )
        return False, "required qualification stages are incomplete"
    return True, None


class ProcessingRetrieval:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        embedder: EmbeddingPort,
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.embedder = embedder

    def build(
        self,
        scope: ProcessingScope,
        records: tuple[tuple[int, ObjectProcessingRecord], ...],
        contexts: Mapping[int, PageIndexContext] | None = None,
    ) -> RetrievalPublication:
        """Embed every eligible member; ``contexts`` gives each page's index-text header."""
        members: list[RetrievalMember] = []
        entries: list[IndexEntry] = []
        for page_index, record in records:
            eligible, _ = eligibility(record)
            if not eligible:
                continue
            stages = {stage.stage: stage for stage in record.stages}
            required = (
                ("qualified_ir", "qualified_description", "qualification", "svg")
                if record.kind is ObjectKind.CHART
                else ("ir", "description", "qualification", "svg")
            )
            refs = tuple(stages[name].artifact for name in required)
            if any(ref is None for ref in refs):
                raise ValueError("Eligible stages lack their actual artifacts")
            ir, description, qualification, svg = refs
            assert (
                ir is not None
                and description is not None
                and qualification is not None
                and svg is not None
            )
            lineage: tuple[AssetRef, ...] = ()
            if record.kind is ObjectKind.CHART:
                lineage_stages: tuple[str, ...] = ("ir", "description", "model_view")
                if uses_displayed_bar_policy(self.outputs.assets.get(qualification)):
                    lineage_stages = (
                        "ir",
                        "description",
                        "description_raw",
                        "model_view",
                        "normalized_description",
                        "normalization_receipt",
                        "source_paint_proof",
                        "page_context_proof",
                    )
                elif "source_paint_proof" in stages:
                    lineage_stages += ("source_paint_proof",)
                raw_refs = tuple(stages.get(name) for name in lineage_stages)
                if any(
                    stage is None
                    or stage.state is not StageState.SUCCEEDED
                    or stage.artifact is None
                    for stage in raw_refs
                ):
                    raise ValueError(
                        "Qualified chart is missing its raw branch, view or source proof lineage"
                    )
                lineage = tuple(
                    stage.artifact
                    for stage in raw_refs
                    if stage is not None and stage.artifact is not None
                )
            provisional = RetrievalMember(
                record.object_id,
                record.kind,
                page_index,
                ir,
                description,
                qualification,
                description,
                svg,
                self.embedder.fingerprint,
                1,
                lineage,
            )
            checked_ir, checked_description, _ = self._qualified(scope, provisional)
            text = contextual_index_text(
                member_index_text(checked_ir, checked_description.text),
                None if contexts is None else contexts.get(page_index),
            )
            embedding_ref, embedding = self._embedding(description, text)
            member = RetrievalMember(
                record.object_id,
                record.kind,
                page_index,
                ir,
                description,
                qualification,
                embedding_ref,
                svg,
                self.embedder.fingerprint,
                len(embedding.vector),
                lineage,
            )
            members.append(member)
            entries.append(IndexEntry(member.member_id, embedding.vector))
        if len({member.embedding_dimensions for member in members}) > 1:
            raise ValueError("One retrieval snapshot cannot mix embedding dimensions")
        plan = RetrievalPlan(scope, tuple(members), _POLICY, _INDEX)
        index = RetrievalIndex(
            plan.snapshot_id,
            _INDEX,
            tuple(sorted(entries, key=lambda entry: entry.member_id)),
        )
        plan_ref = self.outputs.assets.put(
            TypeAdapter(RetrievalPlan).dump_json(plan), media_type="application/json"
        )
        index_ref = self.outputs.assets.put(
            TypeAdapter(RetrievalIndex).dump_json(index), media_type="application/json"
        )
        publication = RetrievalPublication(
            plan.snapshot_id, plan_ref, index_ref, retrieval_dependencies(plan)
        )
        self._load(publication)
        return publication

    def _embedding(self, description: AssetRef, text: str) -> tuple[AssetRef, RetrievalEmbedding]:
        fingerprint = sha256(
            repr(
                (
                    "index-text-embedding-v1",
                    description,
                    sha256(text.encode()).hexdigest(),
                    self.embedder.fingerprint,
                )
            ).encode()
        ).hexdigest()
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            assert cached.artifact is not None
            embedding = TypeAdapter(RetrievalEmbedding).validate_json(
                self.outputs.assets.get(cached.artifact)
            )
            if (
                embedding.description_sha256 != description.sha256
                or embedding.fingerprint != self.embedder.fingerprint
            ):
                raise ValueError("Cached embedding belongs to a different description or model")
            return cached.artifact, embedding
        embedding = RetrievalEmbedding(
            description.sha256,
            self.embedder.fingerprint,
            self.embedder.embed_description(text),
        )
        ref = self.outputs.assets.put(
            TypeAdapter(RetrievalEmbedding).dump_json(embedding),
            media_type="application/json",
        )
        self.outputs.cache(
            StageOutcome(
                "embedding",
                fingerprint,
                StageState.SUCCEEDED,
                self.embedder.fingerprint,
                ref,
            )
        )
        return ref, embedding

    def _load(self, publication: RetrievalPublication) -> tuple[RetrievalPlan, RetrievalIndex]:
        return self.outputs.load_retrieval(publication)

    def search(
        self, publication: RetrievalPublication, query: str, *, limit: int = 5
    ) -> tuple[PinnedRetrievalHit, ...]:
        plan, index = self._load(publication)
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("A nonempty query and bounded limit are required")
        if not plan.members:
            return ()
        if any(
            member.embedding_fingerprint != self.embedder.fingerprint for member in plan.members
        ):
            raise ValueError("Query embedding provider differs from the pinned index")
        query_vector = self.embedder.embed_query(query)
        RetrievalEmbedding("query", self.embedder.fingerprint, query_vector)
        hits: list[PinnedRetrievalHit] = []
        for entry in index.entries:
            if len(query_vector) != len(entry.vector):
                raise ValueError("Query embedding dimensions differ from the pinned index")
            divisor = sqrt(
                sum(value * value for value in query_vector)
                * sum(value * value for value in entry.vector)
            )
            score = (
                sum(left * right for left, right in zip(query_vector, entry.vector, strict=True))
                / divisor
                if divisor
                else 0.0
            )
            hits.append(PinnedRetrievalHit(plan.snapshot_id, entry.member_id, score))
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.member_id))[:limit])

    def resolve(
        self, publication: RetrievalPublication, hit: PinnedRetrievalHit
    ) -> RetrievalContext:
        return resolve_processing_context(self.sources, self.outputs, publication, hit)

    def _literal(
        self, scope: ProcessingScope, member: RetrievalMember
    ) -> tuple[TextIR | ListIR | GroupIR | TableIR, ObjectDescription, LiteralQualification]:
        return validate_literal_member(self.sources, self.outputs.assets, scope, member)

    def _qualified(
        self, scope: ProcessingScope, member: RetrievalMember
    ) -> tuple[
        TextIR | ListIR | GroupIR | TableIR | ChartIR,
        ObjectDescription | TextDescription,
        LiteralQualification | FigureQualification,
    ]:
        if member.kind is ObjectKind.CHART:
            return validate_retrieval_chart_member(self.sources, self.outputs.assets, scope, member)
        return self._literal(scope, member)


def resolve_processing_context(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    publication: RetrievalPublication,
    hit: PinnedRetrievalHit,
) -> RetrievalContext:
    """Hydration verifies stored evidence and never requires an embedding/model call."""
    plan, _ = outputs.load_retrieval(publication)
    member = resolve_member(plan, hit)
    if member.kind is ObjectKind.CHART:
        chart, chart_description, chart_receipt = validate_retrieval_chart_member(
            sources, outputs.assets, plan.scope, member
        )
        return RetrievalContext(plan.snapshot_id, member, chart, chart_description, chart_receipt)
    ir, description, receipt = validate_literal_member(sources, outputs.assets, plan.scope, member)
    return RetrievalContext(plan.snapshot_id, member, ir, description, receipt)
