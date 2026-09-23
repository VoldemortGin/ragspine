"""Rebuild diagram structure qualification from pinned source assets before use."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.diagram_qualification import qualify_diagram
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.objects.diagrams.diagram_models import DiagramQualification
from ragspine.extraction.evidence.objects.typed_ir import DiagramIR, ObjectDescription
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingScope


class DiagramPublicationReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["source-diagram-structure-qualification-v1"] = (
        "source-diagram-structure-qualification-v1"
    )
    object_id: str
    source_manifest_id: str
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    raw_ir: AssetRef
    raw_description: AssetRef
    view: AssetRef
    qualification: DiagramQualification


def parse_diagram_receipt(data: bytes) -> DiagramPublicationReceipt:
    return TypeAdapter(DiagramPublicationReceipt).validate_json(data, strict=True)


@dataclass(frozen=True, slots=True)
class ValidatedDiagramMember:
    ir: DiagramIR
    description: ObjectDescription
    qualification: DiagramQualification


def resolve_diagram_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> ValidatedDiagramMember:
    """No model/network calls: rebuild the crop from the pinned page, then repeat the proof.

    ``DiagramQualificationError`` is a ``ValueError`` and propagates unchanged, so a
    diagram whose structure no longer proves cannot be mounted.
    """
    receipt = parse_diagram_receipt(assets.get(member.qualification))
    if (
        member.kind is not ObjectKind.DIAGRAM
        or member.page_index not in scope.selected_page_indices
        or (
            receipt.object_id,
            receipt.source_manifest_id,
            receipt.ir,
            receipt.description,
            receipt.source_svg,
        )
        != (
            member.object_id,
            scope.source_manifest_id,
            member.ir,
            member.description,
            member.source_svg,
        )
    ):
        raise ValueError("Diagram receipt does not match its retrieval member")
    if (
        set(member.lineage_refs) != {receipt.raw_ir, receipt.raw_description, receipt.view}
        or len(member.lineage_refs) != 3
    ):
        raise ValueError(
            "Diagram publication requires the complete raw branch and model-view closure"
        )
    source = sources.load(scope.source_manifest_id)
    anchor = receipt.qualification.source
    if source.manifest.source.sha256 != scope.source_sha256 or (
        anchor.document_sha256,
        anchor.page_index,
    ) != (scope.source_sha256, member.page_index):
        raise ValueError("Diagram qualification is outside the pinned source scope")
    page = source.manifest.pages[member.page_index]
    expected_crop = crop_native_svg(
        sources.get(page.svg).decode(),
        width=page.width,
        height=page.height,
        bbox=anchor.bbox,
    ).encode()
    if assets.get(member.source_svg) != expected_crop:
        raise ValueError("Diagram SVG crop does not derive from the pinned source page and anchor")
    raw_ir = TypeAdapter(DiagramIR).validate_json(assets.get(receipt.raw_ir), strict=True)
    if raw_ir.source != anchor or raw_ir.object_id != member.object_id:
        raise ValueError("Raw diagram IR is not bound to the qualified anchor")
    spans = read_text_sidecar(sources, source, member.page_index).spans
    expected = qualify_diagram(
        svg=expected_crop,
        spans=spans,
        ir=raw_ir,
        source_manifest_id=scope.source_manifest_id,
    )
    ir = TypeAdapter(DiagramIR).validate_json(assets.get(member.ir), strict=True)
    description = TypeAdapter(ObjectDescription).validate_json(
        assets.get(member.description), strict=True
    )
    if (ir, description, receipt.qualification) != (
        expected.ir,
        expected.description,
        expected.qualification,
    ):
        raise ValueError(
            "Diagram projection or receipt differs from independent source qualification"
        )
    return ValidatedDiagramMember(ir, description, receipt.qualification)


def validate_diagram_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> tuple[DiagramIR, ObjectDescription, DiagramQualification]:
    resolved = resolve_diagram_member(sources, assets, scope, member)
    return resolved.ir, resolved.description, resolved.qualification
