"""Generic draft qualification: diagnose retrievable members without model calls."""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Literal

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.adapters.processing_export import export_processing_review
from enterprise_pdf_rag.adapters.processing_retrieval import (
    ProcessingRetrieval,
    eligibility,
)
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.figures.ports import EmbeddingPort
from enterprise_pdf_rag.processing.models import ObjectKind


class DraftQualification(BoundaryModel):
    source_sha256: str
    source_manifest_id: str
    processing_id: str
    qualification_policy: Literal["retrieval-eligibility-kind-and-stage-completeness-v2"] = (
        "retrieval-eligibility-kind-and-stage-completeness-v2"
    )
    eligible_member_count: int
    skipped_object_count: int
    chart_member_count: int
    kinds: dict[str, int]
    skipped_reasons: dict[str, int]
    indexed: Literal[False] = False
    activated: Literal[False] = False
    retrieval_status: Literal["qualified; indexing pending"] = "qualified; indexing pending"


def qualify_draft(
    *, source_store: Path, processing_store: Path, processing_id: str
) -> DraftQualification:
    """Load a saved draft by id, verify its evidence, and count retrievable members.

    No embedder or model is used. Zero eligible members is legal: a source-only
    draft simply has nothing to index yet.
    """
    sources = LocalDocumentStore(Path(source_store).resolve())
    outputs = ProcessingStore(Path(processing_store).resolve())
    manifest = outputs.load(processing_id)
    plan = None if manifest.retrieval is None else outputs.load_retrieval(manifest.retrieval)[0]
    validate_processing_source(
        sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
    )
    kinds: Counter[str] = Counter()
    skipped_reasons: Counter[str] = Counter()
    eligible_count = 0
    skipped_count = 0
    chart_count = 0
    for page in manifest.pages:
        for record in page.objects:
            eligible, reason = eligibility(record)
            if eligible:
                eligible_count += 1
                kinds[record.kind.value] += 1
                if record.kind is ObjectKind.CHART:
                    chart_count += 1
            else:
                skipped_count += 1
                assert reason is not None
                skipped_reasons[reason] += 1
    return DraftQualification(
        source_sha256=manifest.scope.source_sha256,
        source_manifest_id=manifest.scope.source_manifest_id,
        processing_id=processing_id,
        eligible_member_count=eligible_count,
        skipped_object_count=skipped_count,
        chart_member_count=chart_count,
        kinds=dict(sorted(kinds.items())),
        skipped_reasons=dict(sorted(skipped_reasons.items())),
    )


class DraftIndex(BoundaryModel):
    source_sha256: str
    source_manifest_id: str
    processing_id: str
    indexed_processing_id: str
    retrieval_snapshot_id: str
    member_count: int
    embedding_dimensions: tuple[int, ...]
    embedding_fingerprint: str
    indexed: Literal[True] = True
    activated: Literal[False] = False
    retrieval_status: Literal["indexed; publication pending"] = "indexed; publication pending"
    review_path: str


def index_draft(
    *,
    source_store: Path,
    processing_store: Path,
    processing_id: str,
    embedder: EmbeddingPort,
    document_label: str | None = None,
) -> DraftIndex:
    """Embed a saved draft's eligible index texts into a new immutable snapshot.

    The embedder is injected, never constructed here; only index text (the page's
    contextual header plus the description or chart projection) ever reaches it. A
    fresh snapshot is saved without moving any discovery pointer, so the draft stays
    unactivated. Missing artifacts or mixed dimensions raise
    ValueError from the shared build, failing closed.
    """
    sources = LocalDocumentStore(Path(source_store).resolve())
    outputs = ProcessingStore(Path(processing_store).resolve())
    manifest = outputs.load(processing_id)
    records = tuple((page.page_index, record) for page in manifest.pages for record in page.objects)
    publication = ProcessingRetrieval(sources, outputs, embedder).build(
        manifest.scope, records, outputs.index_contexts(manifest)
    )
    indexed_id = outputs.save_draft(replace(manifest, retrieval=publication), sources=sources)
    plan, _ = outputs.load_retrieval(publication)
    label = (
        document_label
        if document_label is not None
        else sources.load(manifest.scope.source_manifest_id).manifest.filename
    )
    review = export_processing_review(
        sources,
        outputs,
        indexed_id,
        update_current=False,
        title=f"{label} · 检索索引审阅",
    )
    return DraftIndex(
        source_sha256=manifest.scope.source_sha256,
        source_manifest_id=manifest.scope.source_manifest_id,
        processing_id=processing_id,
        indexed_processing_id=indexed_id,
        retrieval_snapshot_id=publication.snapshot_id,
        member_count=len(plan.members),
        embedding_dimensions=tuple(
            sorted({member.embedding_dimensions for member in plan.members})
        ),
        embedding_fingerprint=embedder.fingerprint,
        review_path=str(review),
    )


class DraftPublication(BoundaryModel):
    source_sha256: str
    source_manifest_id: str
    processing_id: str
    published_processing_id: str
    retrieval_snapshot_id: str
    member_count: int
    embedding_dimensions: tuple[int, ...]
    source_store: str
    processing_store: str
    current_processing_id: str
    source_activated: bool
    indexed: Literal[True] = True
    activated: Literal[True] = True
    retrieval_status: Literal["ready"] = "ready"


def publish_draft(
    *,
    source_store: Path,
    processing_store: Path,
    processing_id: str,
    activate_source: bool = True,
) -> DraftPublication:
    """Activate an indexed draft by moving discovery pointers, making no model calls.

    The processing id must name a snapshot that already carries a retrieval index;
    an un-indexed draft raises ValueError, failing closed. Publication re-saves the
    same immutable snapshot (content-addressed, so the id is unchanged) and switches
    ``current-processing`` to it. When ``activate_source`` is true the source
    manifest is also activated via ``current-manifest`` so the whole document becomes
    discoverable. Both moves are idempotent: repeating a publish yields the same ids
    and leaves the pointers in place.
    """
    sources = LocalDocumentStore(Path(source_store).resolve())
    outputs = ProcessingStore(Path(processing_store).resolve())
    manifest = outputs.load(processing_id)
    if manifest.retrieval is None:
        raise ValueError("draft has no retrieval index; run index before publish")
    published_id = outputs.publish(manifest, sources=sources)
    if activate_source:
        sources.activate(manifest.scope.source_manifest_id)
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    return DraftPublication(
        source_sha256=manifest.scope.source_sha256,
        source_manifest_id=manifest.scope.source_manifest_id,
        processing_id=processing_id,
        published_processing_id=published_id,
        retrieval_snapshot_id=manifest.retrieval.snapshot_id,
        member_count=len(plan.members),
        embedding_dimensions=tuple(
            sorted({member.embedding_dimensions for member in plan.members})
        ),
        source_store=str(sources.root),
        processing_store=str(outputs.root),
        current_processing_id=published_id,
        source_activated=activate_source,
    )
