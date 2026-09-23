"""Append an independently qualified description without changing existing vectors."""

from dataclasses import dataclass, replace
from hashlib import sha256

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.bar_publication import (
    BarPublicationCandidate,
    build_displayed_bar_candidate,
    parse_displayed_bar_receipt,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_retrieval import ProcessingRetrieval
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.processing.retrieval import (
    IndexEntry,
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalMember,
    RetrievalPlan,
    retrieval_dependencies,
)
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.figures.chart_qa.models import QueryPin
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ObjectProcessingRecord,
    PagePartition,
    RetrievalPublication,
    StageOutcome,
    StageState,
)

# v2: the appended member's vector embeds the chart index-text projection built by
# ``ProcessingRetrieval`` (ADR 0012); v1 snapshots embedded the description alone.
BAR_PUBLICATION_POLICY = "source-transcription-donut-and-displayed-bar-v2"


def append_displayed_member(
    plan: RetrievalPlan,
    index: RetrievalIndex,
    member: RetrievalMember,
    embedding: RetrievalEmbedding,
) -> tuple[RetrievalPlan, RetrievalIndex]:
    """Mechanical assembly only; source qualification and publication are separate."""
    if (
        not plan.members
        or index.snapshot_id != plan.snapshot_id
        or index.index_version != plan.index_version
        or tuple(entry.member_id for entry in index.entries)
        != tuple(sorted(item.member_id for item in plan.members))
    ):
        raise ValueError("Bar addition requires the complete prior immutable index")
    if member.kind is not ObjectKind.CHART or any(
        (item.page_index, item.object_id) == (member.page_index, member.object_id)
        for item in plan.members
    ):
        raise ValueError("Displayed-bar addition requires a new chart object")
    if {(item.embedding_fingerprint, item.embedding_dimensions) for item in plan.members} != {
        (member.embedding_fingerprint, member.embedding_dimensions)
    } or (
        embedding.description_sha256,
        embedding.fingerprint,
        len(embedding.vector),
    ) != (
        member.description.sha256,
        member.embedding_fingerprint,
        member.embedding_dimensions,
    ):
        raise ValueError(
            "New vector must bind this exact description and the existing model/dimension"
        )
    revised = replace(
        plan,
        members=(*plan.members, member),
        qualification_policy=BAR_PUBLICATION_POLICY,
    )
    return revised, RetrievalIndex(
        revised.snapshot_id,
        index.index_version,
        tuple(
            sorted(
                (*index.entries, IndexEntry(member.member_id, embedding.vector)),
                key=lambda item: item.member_id,
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class DisplayedBarRelease:
    previous_processing_id: str
    previous_snapshot_id: str
    current: QueryPin
    source_manifest_id: str
    qualification_id: str
    publication_receipt: AssetRef
    source_paint_proof: AssetRef
    page_context_proof: AssetRef
    reused_vectors: int
    added_vectors: int
    numeric_claim_count: int


def _qualified_record(
    outputs: ProcessingStore,
    record: ObjectProcessingRecord,
    candidate: BarPublicationCandidate,
) -> ObjectProcessingRecord:
    receipt = parse_displayed_bar_receipt(outputs.assets.get(candidate.qualification))
    updates = {
        "qualified_ir": candidate.ir,
        "qualified_description": candidate.description,
        "qualification": candidate.qualification,
        "normalized_description": receipt.normalized_description,
        "normalization_receipt": receipt.normalization_receipt,
        "source_paint_proof": receipt.source_paint_proof,
        "page_context_proof": receipt.page_context_proof,
    }
    stages = tuple(stage for stage in record.stages if stage.stage not in updates)
    stages += tuple(
        StageOutcome(
            name,
            sha256(
                f"displayed-bar-admission-v1:{candidate.qualification.sha256}:{name}:{ref.sha256}".encode()
            ).hexdigest(),
            StageState.SUCCEEDED,
            "source-displayed-bar-admission-v1",
            ref,
        )
        for name, ref in updates.items()
    )
    return replace(record, stages=stages, qualified_claim_count=candidate.numeric_claim_count)


def create_displayed_bar_draft(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    embedder: EmbeddingPort,
    *,
    processing_id: str,
    page_index: int,
    object_id: str,
) -> DisplayedBarRelease:
    """Requalify source, embed only its independent description, never activate."""
    manifest = outputs.load(processing_id)
    if manifest.retrieval is None:
        raise ValueError("Displayed-bar admission requires an existing retrieval snapshot")
    plan, index = outputs.load_retrieval(manifest.retrieval)
    if not plan.members or {member.embedding_fingerprint for member in plan.members} != {
        embedder.fingerprint
    }:
        raise ValueError("New description must use the existing embedding model fingerprint")
    if any(
        (member.page_index, member.object_id) == (page_index, object_id) for member in plan.members
    ):
        raise ValueError("Displayed-bar admission requires a previously unindexed object")
    validate_processing_source(
        sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
    )
    page = next((page for page in manifest.pages if page.page_index == page_index), None)
    if page is None or page.partition.artifact is None:
        raise ValueError("Displayed-bar object page is absent from the pinned layout")
    partition = TypeAdapter(PagePartition).validate_json(
        outputs.assets.get(page.partition.artifact), strict=True, extra="forbid"
    )
    item = next((item for item in partition.objects if item.object_id == object_id), None)
    record = next((record for record in page.objects if record.object_id == object_id), None)
    if item is None or record is None:
        raise ValueError(
            "Displayed-bar object is absent from the pinned layout or processing stages"
        )
    candidate = build_displayed_bar_candidate(
        sources,
        outputs.assets,
        manifest.scope,
        page_index=page_index,
        item=item,
        record=record,
    )
    qualified = _qualified_record(outputs, record, candidate)
    single = ProcessingRetrieval(sources, outputs, embedder).build(
        manifest.scope, ((page_index, qualified),)
    )
    single_plan, _ = outputs.load_retrieval(single)
    if len(single_plan.members) != 1:
        raise ValueError("Displayed-bar qualification did not produce exactly one member")
    member = single_plan.members[0]
    embedding = TypeAdapter(RetrievalEmbedding).validate_json(
        outputs.assets.get(member.embedding), strict=True, extra="forbid"
    )
    revised_plan, revised_index = append_displayed_member(plan, index, member, embedding)
    publication = RetrievalPublication(
        revised_plan.snapshot_id,
        outputs.assets.put(
            TypeAdapter(RetrievalPlan).dump_json(revised_plan),
            media_type="application/json",
        ),
        outputs.assets.put(
            TypeAdapter(RetrievalIndex).dump_json(revised_index),
            media_type="application/json",
        ),
        retrieval_dependencies(revised_plan),
    )
    revised_page = replace(
        page,
        objects=tuple(qualified if obj.object_id == object_id else obj for obj in page.objects),
    )
    revised = replace(
        manifest,
        pages=tuple(revised_page if p.page_index == page_index else p for p in manifest.pages),
        retrieval=publication,
        producer=f"source-displayed-bar-admission-v1:{processing_id}",
    )
    revised_id = outputs.save_draft(revised, sources=sources)
    receipt = parse_displayed_bar_receipt(outputs.assets.get(candidate.qualification))
    return DisplayedBarRelease(
        processing_id,
        plan.snapshot_id,
        QueryPin(revised_id, revised_plan.snapshot_id, member.member_id),
        manifest.scope.source_manifest_id,
        receipt.qualification.artifact_id,
        candidate.qualification,
        receipt.source_paint_proof,
        receipt.page_context_proof,
        len(index.entries),
        1,
        candidate.numeric_claim_count,
    )
