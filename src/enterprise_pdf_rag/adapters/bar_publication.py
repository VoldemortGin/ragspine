"""Rebuild a displayed-bar member's complete source and independent lineage."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.bar_qualification import (
    QualifiedBarProjection,
    qualify_displayed_bar,
)
from enterprise_pdf_rag.adapters.description_normalization import (
    DescriptionNormalizationReceipt,
    NormalizedDescription,
    normalize_description_evidence,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.figure_reasoning import (
    ContextualFigureModelView,
    FigureModelView,
    PreparedFigure,
    prepare_figure,
)
from enterprise_pdf_rag.adapters.source_paint_bar import (
    SourcePaintBarProof,
    build_bar_source_paint_proof,
    prove_page_context,
    verify_bar_source_paint_proof,
)
from enterprise_pdf_rag.processing.retrieval import RetrievalMember
from ragspine.extraction.evidence.document.models import AssetRef, Bounds
from ragspine.extraction.evidence.figures.chart_qa.displayed_evidence import ACTUAL_FX_CONTEXT
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import (
    DescriptionNormalizationCitation,
    PageContextCitation,
    PointPeriodInterpretation,
    ValidatedDisplayedBar,
)
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    FigureQualification,
    TextDescription,
    Verification,
)
from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectKind,
    ObjectProcessingRecord,
    PageInput,
    ProcessingScope,
    StageState,
)


class BarPublicationReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["source-displayed-bar-qualification-v1"] = (
        "source-displayed-bar-qualification-v1"
    )
    object_id: str
    source_manifest_id: str
    region_id: str
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    raw_chart: AssetRef
    raw_description: AssetRef
    raw_description_json: AssetRef
    view: AssetRef
    normalized_description: AssetRef
    normalization_receipt: AssetRef
    source_paint_proof: AssetRef
    page_context_proof: AssetRef
    qualification: FigureQualification
    point_periods: tuple[PointPeriodInterpretation, ...]
    page_context: tuple[PageContextCitation, ...]
    description_normalization_citation: DescriptionNormalizationCitation
    included_claim_paths: tuple[str, ...]
    excluded_claim_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BarPublicationCandidate:
    ir: AssetRef
    description: AssetRef
    qualification: AssetRef
    source_svg: AssetRef
    lineage_refs: tuple[AssetRef, ...]
    numeric_claim_count: int


def parse_displayed_bar_receipt(data: bytes) -> BarPublicationReceipt:
    return BarPublicationReceipt.model_validate_json(data, strict=True)


def bar_lineage_refs(receipt: BarPublicationReceipt) -> tuple[AssetRef, ...]:
    return (
        receipt.raw_chart,
        receipt.raw_description,
        receipt.raw_description_json,
        receipt.view,
        receipt.normalized_description,
        receipt.normalization_receipt,
        receipt.source_paint_proof,
        receipt.page_context_proof,
    )


def _stage(record: ObjectProcessingRecord, name: str) -> AssetRef:
    matches = tuple(stage for stage in record.stages if stage.stage == name)
    if (
        len(matches) != 1
        or matches[0].state is not StageState.SUCCEEDED
        or matches[0].artifact is None
    ):
        raise ValueError("bar_publication_requires_actual_raw_stage:" + name)
    return matches[0].artifact


@dataclass(frozen=True, slots=True)
class _Inputs:
    pdf: bytes
    page: PageInput
    native_svg: bytes
    source_text: bytes
    prepared: PreparedFigure
    chart: ChartIR
    previous_description: TextDescription
    normalized: NormalizedDescription


def _inputs(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    *,
    page_index: int,
    bbox: Bounds,
    region_id: str,
    source_svg: AssetRef,
    raw_chart: AssetRef,
    raw_description: AssetRef,
    raw_description_json: AssetRef,
    view_ref: AssetRef,
) -> _Inputs:
    snapshot = sources.load(scope.source_manifest_id)
    if (
        snapshot.manifest.source.sha256 != scope.source_sha256
        or len(snapshot.manifest.pages) != scope.source_page_count
        or page_index not in scope.selected_page_indices
    ):
        raise ValueError("displayed_bar_source_scope_mismatch")
    source_page = snapshot.manifest.pages[page_index]
    page = PageInput(
        scope.source_manifest_id,
        scope.source_sha256,
        page_index,
        source_page.width,
        source_page.height,
        source_page.svg,
        read_text_sidecar(sources, snapshot, page_index),
    )
    view: FigureModelView | ContextualFigureModelView = TypeAdapter(
        FigureModelView | ContextualFigureModelView
    ).validate_json(assets.get(view_ref), strict=True, extra="forbid")
    context_ids = (
        tuple(observation.source_span_id for observation in view.page_context)
        if isinstance(view, ContextualFigureModelView)
        else ()
    )
    native = sources.get(source_page.svg)
    prepared = prepare_figure(
        page=page,
        native_svg=native,
        bbox=bbox,
        region_id=region_id,
        context_span_ids=context_ids,
    )
    if prepared.model_view != view or prepared.svg.svg.encode() != assets.get(source_svg):
        raise ValueError("displayed_bar_view_or_svg_source_mismatch")
    chart = TypeAdapter(ChartIR).validate_json(assets.get(raw_chart), strict=True, extra="forbid")
    previous = TypeAdapter(TextDescription).validate_json(
        assets.get(raw_description), strict=True, extra="forbid"
    )
    if (
        chart.binding != prepared.svg.binding
        or previous.binding != prepared.svg.binding
        or chart.verification is not Verification.PENDING
        or previous.verification is not Verification.PENDING
    ):
        raise ValueError("displayed_bar_raw_branch_binding_or_status_mismatch")
    normalized = normalize_description_evidence(
        svg=prepared.svg, raw_json=assets.get(raw_description_json), previous=previous
    )
    return _Inputs(
        sources.get(snapshot.manifest.source),
        page,
        native,
        sources.get(source_page.text),
        prepared,
        chart,
        previous,
        normalized,
    )


def _context(inputs: _Inputs) -> tuple[PageContextCitation, SourcePaintBarProof]:
    spans = tuple(span for span in inputs.page.text.spans if span.text == ACTUAL_FX_CONTEXT)
    if len(spans) != 1:
        raise ValueError("expense_ratio_actual_fx_context_ambiguous_or_missing")
    span = spans[0]
    source = inputs.prepared.svg.source
    if any(element.source_span_id == span.span_id for element in inputs.prepared.svg.elements) or (
        max(span.bbox[0], source.bbox[0]) < min(span.bbox[2], source.bbox[2])
        and max(span.bbox[1], source.bbox[1]) < min(span.bbox[3], source.bbox[3])
    ):
        raise ValueError("page_context_cannot_be_crop_field_evidence")
    return prove_page_context(
        inputs.pdf,
        page=inputs.page,
        native_svg=inputs.native_svg,
        source_text=inputs.source_text,
        source_span_id=span.span_id,
    )


def _normalization_citation(inputs: _Inputs) -> DescriptionNormalizationCitation:
    receipt = inputs.normalized.receipt
    return DescriptionNormalizationCitation(
        receipt.binding,
        receipt.artifact_id,
        receipt.raw_sha256,
        receipt.original_typed_artifact_id,
        receipt.output_description_artifact_id,
        receipt.producer,
    )


def _qualify(inputs: _Inputs, proof: SourcePaintBarProof) -> QualifiedBarProjection:
    return qualify_displayed_bar(
        inputs.prepared,
        raw_chart=inputs.chart,
        description=inputs.normalized.description,
        source_proof=proof,
    )


def build_displayed_bar_candidate(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    *,
    page_index: int,
    item: LayoutObject,
    record: ObjectProcessingRecord,
) -> BarPublicationCandidate:
    """Write only content-addressed evidence after all source checks; never embed."""
    if (record.object_id, record.kind) != (
        item.object_id,
        ObjectKind.CHART,
    ) or item.kind is not ObjectKind.CHART:
        raise ValueError("displayed_bar_object_identity_mismatch")
    raw_chart, raw_description, raw_json, view, svg = tuple(
        _stage(record, name)
        for name in ("ir", "description", "description_raw", "model_view", "svg")
    )
    region_id = item.extraction_region_id or item.object_id
    inputs = _inputs(
        sources,
        assets,
        scope,
        page_index=page_index,
        bbox=item.bbox,
        region_id=region_id,
        source_svg=svg,
        raw_chart=raw_chart,
        raw_description=raw_description,
        raw_description_json=raw_json,
        view_ref=view,
    )
    actual_context = tuple(
        observation.source_span_id for observation in inputs.prepared.page_context
    )
    if actual_context != item.context_span_ids:
        raise ValueError("displayed_bar_layout_context_mismatch")
    proof = build_bar_source_paint_proof(inputs.pdf, prepared=inputs.prepared)
    projection = _qualify(inputs, proof)
    context, context_proof = _context(inputs)

    def put(data: bytes) -> AssetRef:
        return assets.put(data, media_type="application/json")

    ir = put(TypeAdapter(ChartIR).dump_json(projection.chart))
    description = put(TypeAdapter(TextDescription).dump_json(projection.description))
    receipt = BarPublicationReceipt(
        object_id=item.object_id,
        source_manifest_id=scope.source_manifest_id,
        region_id=region_id,
        ir=ir,
        description=description,
        source_svg=svg,
        raw_chart=raw_chart,
        raw_description=raw_description,
        raw_description_json=raw_json,
        view=view,
        normalized_description=put(
            TypeAdapter(TextDescription).dump_json(inputs.normalized.description)
        ),
        normalization_receipt=put(inputs.normalized.receipt.model_dump_json().encode()),
        source_paint_proof=put(TypeAdapter(SourcePaintBarProof).dump_json(proof)),
        page_context_proof=put(TypeAdapter(SourcePaintBarProof).dump_json(context_proof)),
        qualification=projection.qualification,
        point_periods=projection.point_periods,
        page_context=(context,),
        description_normalization_citation=_normalization_citation(inputs),
        included_claim_paths=projection.included_claim_paths,
        excluded_claim_paths=projection.excluded_claim_paths,
    )
    return BarPublicationCandidate(
        ir,
        description,
        put(receipt.model_dump_json().encode()),
        svg,
        bar_lineage_refs(receipt),
        len(projection.chart.points),
    )


def resolve_displayed_bar_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> ValidatedDisplayedBar:
    """Source requalification is mandatory; serialized flags grant no authority."""
    receipt = parse_displayed_bar_receipt(assets.get(member.qualification))
    if member.kind is not ObjectKind.CHART or (
        receipt.object_id,
        receipt.source_manifest_id,
        receipt.ir,
        receipt.description,
        receipt.source_svg,
    ) != (
        member.object_id,
        scope.source_manifest_id,
        member.ir,
        member.description,
        member.source_svg,
    ):
        raise ValueError("displayed_bar_member_dependency_mismatch")
    lineage = bar_lineage_refs(receipt)
    if (
        len(set(lineage)) != 8
        or len(member.lineage_refs) != 8
        or set(member.lineage_refs) != set(lineage)
    ):
        raise ValueError("displayed_bar_eight_asset_lineage_required")
    anchor = receipt.qualification.source
    if (anchor.document_sha256, anchor.source_revision, anchor.page_index) != (
        scope.source_sha256,
        scope.source_sha256,
        member.page_index,
    ):
        raise ValueError("displayed_bar_qualification_source_mismatch")
    inputs = _inputs(
        sources,
        assets,
        scope,
        page_index=member.page_index,
        bbox=anchor.bbox,
        region_id=receipt.region_id,
        source_svg=member.source_svg,
        raw_chart=receipt.raw_chart,
        raw_description=receipt.raw_description,
        raw_description_json=receipt.raw_description_json,
        view_ref=receipt.view,
    )
    normalized = TypeAdapter(TextDescription).validate_json(
        assets.get(receipt.normalized_description), strict=True, extra="forbid"
    )
    normalization = DescriptionNormalizationReceipt.model_validate_json(
        assets.get(receipt.normalization_receipt), strict=True
    )
    if normalized != inputs.normalized.description or normalization != inputs.normalized.receipt:
        raise ValueError("displayed_bar_normalization_revalidation_mismatch")
    recorded = TypeAdapter(SourcePaintBarProof).validate_json(
        assets.get(receipt.source_paint_proof), strict=True, extra="forbid"
    )
    proof = verify_bar_source_paint_proof(inputs.pdf, prepared=inputs.prepared, proof=recorded)
    projection = _qualify(inputs, proof)
    context, context_proof = _context(inputs)
    recorded_context = TypeAdapter(SourcePaintBarProof).validate_json(
        assets.get(receipt.page_context_proof), strict=True, extra="forbid"
    )
    if recorded_context != context_proof:
        raise ValueError("page_context_paint_revalidation_mismatch")
    chart = TypeAdapter(ChartIR).validate_json(assets.get(member.ir), strict=True, extra="forbid")
    description = TypeAdapter(TextDescription).validate_json(
        assets.get(member.description), strict=True, extra="forbid"
    )
    if (
        chart,
        description,
        receipt.qualification,
        receipt.point_periods,
        receipt.page_context,
        receipt.description_normalization_citation,
        receipt.included_claim_paths,
        receipt.excluded_claim_paths,
    ) != (
        projection.chart,
        projection.description,
        projection.qualification,
        projection.point_periods,
        (context,),
        _normalization_citation(inputs),
        projection.included_claim_paths,
        projection.excluded_claim_paths,
    ):
        raise ValueError("displayed_bar_projection_or_receipt_revalidation_mismatch")
    return ValidatedDisplayedBar(
        inputs.chart,
        chart,
        description,
        projection.qualification,
        inputs.prepared.svg,
        projection.point_periods,
        (context,),
        _normalization_citation(inputs),
    )
