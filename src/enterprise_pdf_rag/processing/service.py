"""Pure transforms and coverage checks for source-bound page processing."""

from dataclasses import replace
from math import isfinite

from enterprise_pdf_rag.figures.models import SourceAnchor, content_id
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.models import (
    CanonicalPage,
    CanonicalText,
    PageInput,
    PagePartition,
)


def canonical_page(page: PageInput) -> CanonicalPage:
    return CanonicalPage(
        "canonical-source-v1",
        page.source_manifest_id,
        page.source_sha256,
        page.page_index,
        content_id("source-page-v1", (page.source_sha256, page.page_index)),
        page.native_svg,
        tuple(
            CanonicalText(
                content_id("source-text-v1", (page.source_sha256, span.span_id)),
                span.span_id,
                SourceAnchor(
                    page.source_sha256,
                    page.source_sha256,
                    page.page_index,
                    span.bbox,
                ),
                span.text,
            )
            for span in page.text.spans
        ),
    )


def validate_partition(page: PageInput, partition: PagePartition) -> None:
    if (
        partition.source_manifest_id,
        partition.source_sha256,
        partition.page_index,
    ) != (page.source_manifest_id, page.source_sha256, page.page_index):
        raise ValueError("Layout enrichment points to a different source page")
    object_ids = tuple(item.object_id for item in partition.objects)
    if len(set(object_ids)) != len(object_ids) or not all(object_ids):
        raise ValueError("Layout object IDs must be unique")
    observed = {span.span_id for span in page.text.spans}
    ownership = (
        *partition.unassigned_span_ids,
        *(span_id for item in partition.objects for span_id in item.source_span_ids),
    )
    if len(set(ownership)) != len(ownership):
        raise ValueError("Local source occurrence ownership must be unique")
    covered = set(partition.unassigned_span_ids)
    parents = {item.object_id: item.parent_id for item in partition.objects}
    for item in partition.objects:
        if not all(isfinite(value) for value in item.bbox) or not contains(
            (0.0, 0.0, page.width, page.height), item.bbox
        ):
            raise ValueError("Layout region is outside source page geometry")
        if item.parent_id is not None and item.parent_id not in object_ids:
            raise ValueError("Layout parent does not exist on this page")
        context = set(item.context_span_ids)
        if (
            len(context) != len(item.context_span_ids)
            or not context.issubset(observed)
            or context.intersection(item.source_span_ids)
        ):
            raise ValueError("Page context must use distinct observed occurrences")
        visited = {item.object_id}
        parent = item.parent_id
        while parent is not None:
            if parent in visited:
                raise ValueError("Layout parent relationships contain a cycle")
            visited.add(parent)
            parent = parents[parent]
        covered.update(item.source_span_ids)
    if covered != observed:
        raise ValueError("Layout must account for all source text occurrences exactly")


def preserve_omitted_occurrences(page: PageInput, partition: PagePartition) -> PagePartition:
    """Retain model omissions explicitly without guessing their semantic owner."""
    observed = {span.span_id for span in page.text.spans}
    assigned = (
        *partition.unassigned_span_ids,
        *(span_id for item in partition.objects for span_id in item.source_span_ids),
    )
    if not set(assigned).issubset(observed):
        raise ValueError("Model layout contains unknown source occurrences")
    if len(set(assigned)) != len(assigned):
        raise ValueError("Local source occurrence ownership must be unique")
    missing = tuple(span.span_id for span in page.text.spans if span.span_id not in assigned)
    return replace(
        partition,
        unassigned_span_ids=(*partition.unassigned_span_ids, *missing),
        diagnostics=(
            *partition.diagnostics,
            *(
                f"Model omitted source occurrence {span_id}; preserved as explicitly unassigned, no relationship inferred."
                for span_id in missing
            ),
        ),
    )
