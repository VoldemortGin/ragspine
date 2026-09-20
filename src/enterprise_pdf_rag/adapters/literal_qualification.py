"""Independently revalidate exact literal projections against their pinned source."""

from collections.abc import Sequence

import pdfspine
from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.pdfspine_tables import fill_rectangles, ruling_segments
from enterprise_pdf_rag.documents.models import TextSpan
from enterprise_pdf_rag.figures.models import Confidence, Verification
from enterprise_pdf_rag.processing.geometry import contains
from enterprise_pdf_rag.processing.models import ObjectKind, ProcessingScope
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from enterprise_pdf_rag.processing.table_grid_proof import GRID_SCOPE, check_grid_evidence
from enterprise_pdf_rag.processing.table_models import TableIR
from enterprise_pdf_rag.processing.table_transcription import (
    check_table_transcription,
    table_span_ids,
)
from enterprise_pdf_rag.processing.typed_ir import (
    GroupIR,
    ListIR,
    LiteralQualification,
    ObjectDescription,
    TextIR,
)

LITERAL_SCOPE = "literal-source-transcription-v1"


def _reprove_table_grid(
    pdf: bytes,
    table: TableIR,
    receipt: LiteralQualification,
    *,
    page_index: int,
    spans: Sequence[TextSpan],
) -> None:
    """Re-prove a VERIFIED grid from the pinned page's own rulings (ADR 0014).

    A pending grid must not claim one: the receipt's two grid fields are then absent.
    """
    evidence = table.grid_evidence
    if table.verification is not Verification.VERIFIED or evidence is None:
        if receipt.grid_scope is not None or receipt.ruling_digest is not None:
            raise ValueError("Pending table grid must not carry a grid qualification")
        return
    if (receipt.grid_scope, receipt.ruling_digest) != (GRID_SCOPE, evidence.ruling_digest):
        raise ValueError("Table grid qualification does not bind the proved rulings")
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        if page_index >= document.page_count:
            raise ValueError("Qualified table page is absent from the pinned source")
        source_page = document.load_page(page_index)
        check_grid_evidence(
            table,
            ruling_segments(source_page),
            fills=fill_rectangles(source_page),
            spans=spans,
        )
    finally:
        document.close()


def validate_literal_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> tuple[TextIR | ListIR | GroupIR | TableIR, ObjectDescription, LiteralQualification]:
    receipt = TypeAdapter(LiteralQualification).validate_json(assets.get(member.qualification))
    description = TypeAdapter(ObjectDescription).validate_json(assets.get(member.description))
    if description.producer != "exact-source-transcription-v1":
        raise ValueError("Literal qualification requires the exact transcription producer")
    if description.confidence != Confidence(
        None, "deterministic source occurrence transcription; no semantic inference"
    ):
        raise ValueError(
            "Literal projection confidence must not imply semantic or financial verification"
        )
    source = sources.load(scope.source_manifest_id)
    if (
        source.manifest.source.sha256 != scope.source_sha256
        or member.page_index not in scope.selected_page_indices
    ):
        raise ValueError("Literal projection is outside the pinned source scope")
    text = read_text_sidecar(sources, source, member.page_index)
    spans = {span.span_id: span for span in text.spans}
    if (
        receipt.object_id,
        receipt.source_manifest_id,
        receipt.ir,
        receipt.description,
        receipt.source_svg,
        receipt.scope,
    ) != (
        member.object_id,
        scope.source_manifest_id,
        member.ir,
        member.description,
        member.source_svg,
        LITERAL_SCOPE,
    ):
        raise ValueError("Literal qualification does not bind the exact object dependencies")
    anchor = receipt.source
    if (anchor.coordinate_frame, anchor.rotation, anchor.transform) != (
        "page-top-left",
        0,
        (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    ):
        raise ValueError("Literal source anchor has an unsupported coordinate transform")
    if (anchor.document_sha256, anchor.source_revision, anchor.page_index) != (
        scope.source_sha256,
        scope.source_sha256,
        member.page_index,
    ):
        raise ValueError("Literal qualification has a different source anchor")
    if (
        description.object_id,
        description.source,
        description.source_span_ids,
        description.verification,
    ) != (member.object_id, anchor, receipt.source_span_ids, Verification.VERIFIED):
        raise ValueError("Description does not match its scoped qualification")
    if (
        not receipt.source_span_ids
        or len(set(receipt.source_span_ids)) != len(receipt.source_span_ids)
        or any(span_id not in spans for span_id in receipt.source_span_ids)
    ):
        raise ValueError("Literal qualification references unknown source occurrences")
    if description.text != "\n".join(spans[span_id].text for span_id in receipt.source_span_ids):
        raise ValueError("Qualified text is not an exact source transcription")
    for span_id in receipt.source_span_ids:
        if not contains(anchor.bbox, spans[span_id].bbox):
            raise ValueError("Literal source occurrence is outside its qualified anchor")
    page = source.manifest.pages[member.page_index]
    expected_crop = crop_native_svg(
        sources.get(page.svg).decode(),
        width=page.width,
        height=page.height,
        bbox=anchor.bbox,
    ).encode()
    if assets.get(member.source_svg) != expected_crop:
        raise ValueError("Literal SVG crop does not derive from the pinned source page and anchor")
    payload = assets.get(member.ir)
    if member.kind is ObjectKind.TABLE:
        table = TypeAdapter(TableIR).validate_json(payload)
        if (
            table.object_id != member.object_id
            or (
                table.source.document_sha256,
                table.source.source_revision,
                table.source.page_index,
            )
            != (scope.source_sha256, scope.source_sha256, member.page_index)
            or table_span_ids(table) != receipt.source_span_ids
        ):
            raise ValueError("Typed table IR does not match the literal qualification")
        check_table_transcription(table, spans, anchor=anchor.bbox)
        _reprove_table_grid(
            sources.get(source.manifest.source),
            table,
            receipt,
            page_index=member.page_index,
            spans=text.spans,
        )
        return table, description, receipt
    ir: TextIR | ListIR | GroupIR
    if member.kind is ObjectKind.TEXT:
        ir = TypeAdapter(TextIR).validate_json(payload)
    elif member.kind is ObjectKind.LIST:
        ir = TypeAdapter(ListIR).validate_json(payload)
    elif member.kind is ObjectKind.GROUP:
        ir = TypeAdapter(GroupIR).validate_json(payload)
    else:
        raise ValueError("Unsupported qualification for this object kind")
    if (
        ir.object_id != member.object_id
        or ir.source != anchor
        or tuple(fragment.source_span_id for fragment in ir.fragments) != receipt.source_span_ids
    ):
        raise ValueError("Typed IR does not match the literal qualification")
    for fragment in ir.fragments:
        span = spans[fragment.source_span_id]
        if (
            fragment.text != span.text
            or fragment.source.bbox != span.bbox
            or (
                fragment.source.document_sha256,
                fragment.source.source_revision,
                fragment.source.page_index,
            )
            != (scope.source_sha256, scope.source_sha256, member.page_index)
        ):
            raise ValueError("Typed IR source occurrence was altered")
    return ir, description, receipt
