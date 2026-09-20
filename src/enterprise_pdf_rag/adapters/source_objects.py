"""Ground literal text projections separately from inferred financial relationships."""

from enterprise_pdf_rag.figures.models import Confidence, SourceAnchor, Verification
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.models import LayoutObject, ObjectKind, PageInput
from enterprise_pdf_rag.processing.table_models import TableIR
from enterprise_pdf_rag.processing.table_transcription import (
    check_table_transcription,
    table_span_ids,
)
from enterprise_pdf_rag.processing.typed_ir import (
    GroupIR,
    ListIR,
    ObjectDescription,
    ObservedText,
    SourceObjectResult,
    TextIR,
)

_LITERAL_PRODUCER = "exact-source-transcription-v1"
_LITERAL_CONFIDENCE = Confidence(
    None, "deterministic source occurrence transcription; no semantic inference"
)


def source_object_ir(page: PageInput, item: LayoutObject) -> SourceObjectResult:
    if item.kind not in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP):
        raise ValueError("This producer only transcribes Text/List/Group source observations")
    observed = {span.span_id: span for span in page.text.spans}
    source = SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox)
    fragments: list[ObservedText] = []
    for span_id in item.source_span_ids:
        span = observed.get(span_id)
        if span is None or not contains(item.bbox, span.bbox):
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
            span for span in page.text.spans if contains(item.bbox, span.bbox)
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
        _LITERAL_PRODUCER,
        _LITERAL_CONFIDENCE,
        Verification.VERIFIED,
    )
    return SourceObjectResult(item.kind, source, ir, description)


def source_table_description(
    page: PageInput, item: LayoutObject, table: TableIR
) -> ObjectDescription:
    """Transcribe a native table's cells verbatim, in cell order, as its only description.

    The description is source text alone (no rows, columns or cell ids), so only
    natural language is ever embedded. Any occurrence the layout owns outside the
    native cells, or any cell text that drifts from its occurrences, refuses.
    """
    if item.kind is not ObjectKind.TABLE or table.object_id != item.object_id:
        raise ValueError("This producer only transcribes the Table object's own native grid")
    span_ids = table_span_ids(table)
    if not span_ids:
        raise ValueError("No literal source text is available for this table")
    if set(span_ids) != set(item.source_span_ids):
        raise ValueError("Table layout owns source occurrences outside its native cells")
    observed = {span.span_id: span for span in page.text.spans}
    check_table_transcription(table, observed, anchor=item.bbox)
    return ObjectDescription(
        item.object_id,
        SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox),
        span_ids,
        "\n".join(observed[span_id].text for span_id in span_ids),
        _LITERAL_PRODUCER,
        _LITERAL_CONFIDENCE,
        Verification.VERIFIED,
    )
