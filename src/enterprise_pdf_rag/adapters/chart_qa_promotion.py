"""Create a new numeric release while reusing unchanged description vectors."""

from dataclasses import dataclass, replace
from hashlib import sha256

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.retrieval import (
    IndexEntry,
    RetrievalIndex,
    RetrievalMember,
    RetrievalPlan,
    retrieval_dependencies,
)
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.figures.chart_qa.models import QueryPin
from ragspine.extraction.evidence.page.models import (
    ObjectProcessingRecord,
    RetrievalPublication,
    StageOutcome,
    StageState,
)

PROMOTION_POLICY = "source-transcription-and-numeric-paint-qualification-v1"


def replace_index_member(
    plan: RetrievalPlan,
    index: RetrievalIndex,
    old_member_id: str,
    member: RetrievalMember,
) -> tuple[RetrievalPlan, RetrievalIndex]:
    """Only rebind IDs; a changed description/model may not reuse this vector."""
    old = next((item for item in plan.members if item.member_id == old_member_id), None)
    if old is None or index.snapshot_id != plan.snapshot_id:
        raise ValueError("Promotion requires the exact prior member and snapshot")
    if (
        old.object_id,
        old.kind,
        old.page_index,
        old.description,
        old.embedding,
        old.source_svg,
        old.embedding_fingerprint,
        old.embedding_dimensions,
    ) != (
        member.object_id,
        member.kind,
        member.page_index,
        member.description,
        member.embedding,
        member.source_svg,
        member.embedding_fingerprint,
        member.embedding_dimensions,
    ):
        raise ValueError(
            "Promotion requires unchanged source, description and embedding identities"
        )
    indexed = {entry.member_id: entry.vector for entry in index.entries}
    if set(indexed) != {item.member_id for item in plan.members} or len(indexed) != len(
        index.entries
    ):
        raise ValueError("Prior index does not cover the exact immutable members")
    revised = replace(
        plan,
        members=tuple(member if item.member_id == old_member_id else item for item in plan.members),
        qualification_policy=PROMOTION_POLICY,
    )
    vectors = {
        member.member_id if key == old_member_id else key: value for key, value in indexed.items()
    }
    return revised, RetrievalIndex(
        revised.snapshot_id,
        index.index_version,
        tuple(IndexEntry(key, vectors[key]) for key in sorted(vectors)),
    )


@dataclass(frozen=True, slots=True)
class NumericRelease:
    previous: QueryPin
    current: QueryPin
    source_manifest_id: str
    qualification_id: str
    publication_receipt: AssetRef
    source_paint_proof: AssetRef
    reused_vectors: int
    numeric_claim_count: int


def _promoted_record(
    record: ObjectProcessingRecord,
    member: RetrievalMember,
    proof: AssetRef,
    claim_count: int,
) -> ObjectProcessingRecord:
    updates = {
        "qualified_ir": member.ir,
        "qualification": member.qualification,
        "source_paint_proof": proof,
    }
    if not {"qualified_ir", "qualification"}.issubset({stage.stage for stage in record.stages}):
        raise ValueError("Promotion requires existing qualified chart stages")
    stages = tuple(stage for stage in record.stages if stage.stage not in updates)
    stages += tuple(
        StageOutcome(
            name,
            sha256(
                f"numeric-promotion-v1:{member.member_id}:{name}:{ref.sha256}".encode()
            ).hexdigest(),
            StageState.SUCCEEDED,
            "source-numeric-promotion-v1",
            ref,
        )
        for name, ref in updates.items()
    )
    return replace(record, stages=stages, qualified_claim_count=claim_count)


def create_numeric_draft(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    *,
    processing_id: str,
    member_id: str,
) -> NumericRelease:
    """Source-qualify one chart, preserve all vectors, and leave current untouched."""
    from enterprise_pdf_rag.adapters.chart_publication import (
        NumericLabelPublicationReceipt,
        parse_chart_receipt,
        promote_numeric_label_member,
        resolve_chart_member,
    )

    manifest = outputs.load(processing_id)
    if manifest.retrieval is None:
        raise ValueError("Numeric promotion requires an existing retrieval snapshot")
    plan, index = outputs.load_retrieval(manifest.retrieval)
    old = next((item for item in plan.members if item.member_id == member_id), None)
    if old is None:
        raise ValueError("Promotion member is absent from the fixed retrieval snapshot")
    promoted = promote_numeric_label_member(sources, outputs.assets, manifest.scope, old)
    receipt = parse_chart_receipt(outputs.assets.get(promoted.qualification))
    if not isinstance(receipt, NumericLabelPublicationReceipt):
        raise ValueError("Promotion requires the source-paint numeric publication policy")
    validated = resolve_chart_member(sources, outputs.assets, manifest.scope, promoted)
    revised_plan, revised_index = replace_index_member(plan, index, member_id, promoted)
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
    claim_count = len(validated.chart.points)
    pages = tuple(
        replace(
            page,
            objects=tuple(
                _promoted_record(record, promoted, receipt.source_paint_proof, claim_count)
                if (page.page_index, record.object_id) == (promoted.page_index, promoted.object_id)
                else record
                for record in page.objects
            ),
        )
        for page in manifest.pages
    )
    revised_manifest = replace(
        manifest,
        pages=pages,
        retrieval=publication,
        producer=f"source-numeric-promotion-v1:{processing_id}",
    )
    revised_id = outputs.save_draft(revised_manifest, sources=sources)
    return NumericRelease(
        QueryPin(processing_id, plan.snapshot_id, member_id),
        QueryPin(revised_id, revised_plan.snapshot_id, promoted.member_id),
        manifest.scope.source_manifest_id,
        validated.qualification.artifact_id,
        promoted.qualification,
        receipt.source_paint_proof,
        len(index.entries),
        claim_count,
    )
