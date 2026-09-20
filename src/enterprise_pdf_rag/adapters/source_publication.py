"""Source-aware admission runs before changing the local processing pointer."""

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.chart_member_validation import (
    validate_retrieval_chart_member,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.layout_normalization import normalize_partition
from enterprise_pdf_rag.adapters.literal_qualification import validate_literal_member
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    ObjectKind,
    PageInput,
    PagePartition,
    ProcessingManifest,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalPlan
from enterprise_pdf_rag.processing.service import canonical_page, validate_partition


def validate_processing_source(
    *,
    sources: LocalDocumentStore,
    artifacts: LocalDocumentStore,
    manifest: ProcessingManifest,
    plan: RetrievalPlan | None,
) -> None:
    source = sources.load(manifest.scope.source_manifest_id)
    if (
        source.manifest.source.sha256 != manifest.scope.source_sha256
        or len(source.manifest.pages) != manifest.scope.source_page_count
    ):
        raise ValueError("Processing source identity or page count differs from its manifest")
    for record in manifest.pages:
        source_page = source.manifest.pages[record.page_index]
        page = PageInput(
            manifest.scope.source_manifest_id,
            manifest.scope.source_sha256,
            record.page_index,
            source_page.width,
            source_page.height,
            source_page.svg,
            read_text_sidecar(sources, source, record.page_index),
        )
        if record.canonical.state is not StageState.SUCCEEDED or record.canonical.artifact is None:
            raise ValueError("Processing publication requires actual Canonical source observations")
        canonical = TypeAdapter(CanonicalPage).validate_json(
            artifacts.get(record.canonical.artifact)
        )
        if canonical != canonical_page(page):
            raise ValueError("Canonical observations differ from the pinned source page")
        if record.partition.state is not StageState.SUCCEEDED:
            if record.objects:
                raise ValueError("Objects cannot hide behind a failed layout stage")
            continue
        if record.partition.artifact is None:
            raise ValueError("Successful layout stage has no actual artifact")
        partition = TypeAdapter(PagePartition).validate_json(
            artifacts.get(record.partition.artifact)
        )
        validate_partition(page, partition)
        if record.raw_partition is not None:
            raw_ref = record.raw_partition.artifact
            if record.raw_partition.state is not StageState.SUCCEEDED or raw_ref is None:
                raise ValueError("Normalized layout requires its actual raw model layout")
            raw = TypeAdapter(PagePartition).validate_json(artifacts.get(raw_ref))
            if normalize_partition(page=page, partition=raw) != partition:
                raise ValueError("Normalized layout differs from its pinned raw layout and rules")
        expected = {(item.object_id, item.kind) for item in partition.objects}
        actual = {(item.object_id, item.kind) for item in record.objects}
        if expected != actual or len(record.objects) != len(actual):
            raise ValueError("Processing objects do not cover their actual layout exactly")
    if plan is not None:
        if plan.scope != manifest.scope:
            raise ValueError("Retrieval scope differs from processing scope")
        for member in plan.members:
            if member.kind is ObjectKind.CHART:
                validate_retrieval_chart_member(sources, artifacts, manifest.scope, member)
            else:
                validate_literal_member(sources, artifacts, manifest.scope, member)
