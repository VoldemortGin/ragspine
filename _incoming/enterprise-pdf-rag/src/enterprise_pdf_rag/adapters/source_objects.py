"""Ground literal text projections separately from inferred financial relationships."""

from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.typed_ir import (
    GroupIR,
    ListIR,
    ObjectDescription,
    ObservedText,
    SourceObjectResult,
    TextIR,
)


def source_object_ir(page: PageInput, item: LayoutObject) -> SourceObjectResult:
    if item.kind not in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP):
        raise ValueError(
            "This producer only transcribes Text/List/Group source observations"
        )
    observed = {span.span_id: span for span in page.text.spans}
    source = SourceAnchor(
        page.source_sha256, page.source_sha256, page.page_index, item.bbox
    )
    fragments: list[ObservedText] = []
    for span_id in item.source_span_ids:
        span = observed.get(span_id)
        if span is None or not (
            item.bbox[0] <= span.bbox[0] < span.bbox[2] <= item.bbox[2]
            and item.bbox[1] <= span.bbox[1] < span.bbox[3] <= item.bbox[3]
        ):
            raise ValueError("Text projection contains an unbound source occurrence")
        fragments.append(
            ObservedText(
                span_id,
                span.text,
                SourceAnchor(
                    page.source_sha256,
                    page.source_sha256,
                    page.page_index,
                    span.bbox,
                ),
            )
        )
    if not fragments and item.kind is ObjectKind.GROUP and item.child_object_ids:
        region_observations = tuple(
            span
            for span in page.text.spans
            if item.bbox[0] <= span.bbox[0] < span.bbox[2] <= item.bbox[2]
            and item.bbox[1] <= span.bbox[1] < span.bbox[3] <= item.bbox[3]
        )
        return SourceObjectResult(
            item.kind,
            source,
            GroupIR(
                item.object_id,
                source,
                item.child_object_ids,
                (),
                (
                    "Layout container retains child references; grouping and financial relationships remain unverified",
                ),
            ),
            ObjectDescription(
                item.object_id,
                source,
                tuple(span.span_id for span in region_observations),
                "Source text observed inside this proposed group region; relationships are unverified:\n"
                + "\n".join(span.text for span in region_observations),
                "group-region-transcription-v1",
                Confidence(
                    None,
                    "source-region transcription; inferred grouping is not independently verified",
                ),
                Verification.PENDING,
            ),
        )
    if not fragments:
        raise ValueError("No literal source text is available for this object")
    values = tuple(fragments)
    ir: TextIR | ListIR | GroupIR
    if item.kind is ObjectKind.TEXT:
        ir = TextIR(item.object_id, source, values)
    elif item.kind is ObjectKind.LIST:
        ir = ListIR(
            item.object_id,
            source,
            values,
            item.list_item_span_ids,
            item.list_ordered,
            (
                "Source fragments retained; semantic list item grouping and order are not independently verified",
            ),
        )
    else:
        ir = GroupIR(
            item.object_id,
            source,
            item.child_object_ids,
            values,
            (
                "Source fragments retained; KPI label/value relationships are not independently verified",
            ),
        )
    description = ObjectDescription(
        item.object_id,
        source,
        item.source_span_ids,
        "\n".join(fragment.text for fragment in fragments),
        "exact-source-transcription-v1",
        Confidence(
            None, "deterministic source occurrence transcription; no semantic inference"
        ),
        Verification.VERIFIED,
    )
    return SourceObjectResult(item.kind, source, ir, description)
