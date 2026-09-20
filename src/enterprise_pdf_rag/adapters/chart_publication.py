"""Rebuild chart qualification from pinned source assets before publication/use."""

from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.donut_qualification import DonutQualification
from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_LABEL_SCOPE,
    qualify_source_labels,
)
from enterprise_pdf_rag.adapters.figure_reasoning import (
    ContextualFigureModelView,
    FigureModelView,
    PreparedFigure,
    prepare_figure,
)
from enterprise_pdf_rag.adapters.source_paint import (
    SourcePaintProof,
    build_source_paint_proof,
    verify_source_paint_proof,
)
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    FigureQualification,
    SvgArtifact,
    TextDescription,
    Verification,
)
from enterprise_pdf_rag.figures.validation import validate_pair
from enterprise_pdf_rag.processing.models import ObjectKind, PageInput, ProcessingScope
from enterprise_pdf_rag.processing.retrieval import RetrievalMember


class _ChartReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    object_id: str
    source_manifest_id: str
    region_id: str
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    raw_chart: AssetRef
    raw_description: AssetRef
    view: AssetRef
    qualification: FigureQualification


class ChartPublicationReceipt(_ChartReceipt):
    schema_version: Literal["source-chart-qualification-v1"] = "source-chart-qualification-v1"


class NumericLabelPublicationReceipt(_ChartReceipt):
    source_paint_proof: AssetRef
    schema_version: Literal["source-chart-numeric-label-index-v1"] = (
        "source-chart-numeric-label-index-v1"
    )


type ChartReceipt = ChartPublicationReceipt | NumericLabelPublicationReceipt


def parse_chart_receipt(data: bytes) -> ChartReceipt:
    return TypeAdapter(ChartPublicationReceipt | NumericLabelPublicationReceipt).validate_json(
        data, strict=True
    )


@dataclass(frozen=True, slots=True)
class ValidatedChartMember:
    chart: ChartIR
    description: TextDescription
    qualification: FigureQualification
    svg: SvgArtifact


def resolve_chart_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> ValidatedChartMember:
    """No model/network calls: reconstruct source, then repeat the scoped proof."""
    receipt = parse_chart_receipt(assets.get(member.qualification))
    semantic_scope = receipt.qualification.semantic_scope
    if semantic_scope not in {"explicit-distribution-shares", FIGURE_LABEL_SCOPE}:
        raise ValueError("Unsupported chart qualification scope")
    if (isinstance(receipt, NumericLabelPublicationReceipt)) != (
        semantic_scope == "explicit-distribution-shares"
    ):
        raise ValueError("Numeric chart qualification requires the source-paint publication policy")
    if (
        member.kind is not ObjectKind.CHART
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
        or not receipt.region_id
    ):
        raise ValueError("Chart qualification differs from the pinned object dependencies")
    expected_lineage = {receipt.raw_chart, receipt.raw_description, receipt.view}
    if isinstance(receipt, NumericLabelPublicationReceipt):
        expected_lineage.add(receipt.source_paint_proof)
    if (
        len(member.lineage_refs) != len(expected_lineage)
        or set(member.lineage_refs) != expected_lineage
        or len(expected_lineage)
        != (4 if isinstance(receipt, NumericLabelPublicationReceipt) else 3)
    ):
        raise ValueError(
            "Chart publication requires the complete raw branch and model-view closure"
        )
    prepared, raw_chart, raw_description = _load_chart_inputs(
        sources, assets, scope, member, receipt
    )
    if semantic_scope == FIGURE_LABEL_SCOPE:
        labels = qualify_source_labels(prepared.svg, raw_chart, raw_description)
        expected = (labels.chart, labels.description, labels.receipt)
    else:
        if not isinstance(receipt, NumericLabelPublicationReceipt):
            raise ValueError("Numeric source proof is missing")
        recorded = TypeAdapter(SourcePaintProof).validate_json(
            assets.get(receipt.source_paint_proof), strict=True, extra="forbid"
        )
        source = sources.load(scope.source_manifest_id)
        proof = verify_source_paint_proof(
            sources.get(source.manifest.source), prepared=prepared, proof=recorded
        )
        numeric = DonutQualification(prepared, source_paint=proof).qualify_pair(
            prepared.svg, raw_chart, raw_description
        )
        labels = qualify_source_labels(prepared.svg, raw_chart, raw_description)
        expected = (numeric.chart, labels.description, numeric.receipt)
    chart = TypeAdapter(ChartIR).validate_json(assets.get(member.ir), strict=True)
    description = TypeAdapter(TextDescription).validate_json(
        assets.get(member.description), strict=True
    )
    if (chart, description, receipt.qualification) != expected:
        raise ValueError(
            "Chart projection or receipt differs from independent source qualification"
        )
    if semantic_scope == "explicit-distribution-shares":
        validate_pair(prepared.svg, chart, description)
    return ValidatedChartMember(chart, description, expected[2], prepared.svg)


def _load_chart_inputs(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
    receipt: ChartReceipt,
) -> tuple[PreparedFigure, ChartIR, TextDescription]:
    source = sources.load(scope.source_manifest_id)
    anchor = receipt.qualification.source
    if (
        source.manifest.source.sha256 != scope.source_sha256
        or len(source.manifest.pages) != scope.source_page_count
        or (anchor.document_sha256, anchor.source_revision, anchor.page_index)
        != (scope.source_sha256, scope.source_sha256, member.page_index)
    ):
        raise ValueError("Chart qualification is outside the pinned source scope")
    page_record = source.manifest.pages[member.page_index]
    page = PageInput(
        scope.source_manifest_id,
        scope.source_sha256,
        member.page_index,
        page_record.width,
        page_record.height,
        page_record.svg,
        read_text_sidecar(sources, source, member.page_index),
    )
    view: FigureModelView | ContextualFigureModelView = TypeAdapter(
        FigureModelView | ContextualFigureModelView
    ).validate_json(assets.get(receipt.view), strict=True, extra="forbid")
    context_ids = (
        tuple(observation.source_span_id for observation in view.page_context)
        if isinstance(view, ContextualFigureModelView)
        else ()
    )
    prepared = prepare_figure(
        page=page,
        native_svg=sources.get(page_record.svg),
        bbox=anchor.bbox,
        region_id=receipt.region_id,
        context_span_ids=context_ids,
    )
    if view != prepared.model_view or assets.get(member.source_svg) != prepared.svg.svg.encode():
        raise ValueError("Chart SVG or model view differs from the pinned source derivation")
    raw_chart = TypeAdapter(ChartIR).validate_json(assets.get(receipt.raw_chart), strict=True)
    raw_description = TypeAdapter(TextDescription).validate_json(
        assets.get(receipt.raw_description), strict=True
    )
    if (
        raw_chart.verification is Verification.REJECTED
        or raw_description.verification is Verification.REJECTED
    ):
        raise ValueError("Rejected raw chart branches cannot be published")
    return prepared, raw_chart, raw_description


def promote_numeric_label_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> RetrievalMember:
    """Write immutable proof/projections; preserve the old description/vector.

    This is a candidate constructor. It neither publishes nor changes an active
    pointer, and it has no provider, embedding or retrieval-index dependency.
    """
    old = resolve_chart_member(sources, assets, scope, member)
    receipt = parse_chart_receipt(assets.get(member.qualification))
    if (
        not isinstance(receipt, ChartPublicationReceipt)
        or old.qualification.semantic_scope != FIGURE_LABEL_SCOPE
    ):
        raise ValueError("Promotion requires a verified source-labels-only member")
    prepared, raw_chart, raw_description = _load_chart_inputs(
        sources, assets, scope, member, receipt
    )
    source = sources.load(scope.source_manifest_id)
    proof = build_source_paint_proof(sources.get(source.manifest.source), prepared=prepared)
    numeric = DonutQualification(prepared, source_paint=proof).qualify_pair(
        prepared.svg, raw_chart, raw_description
    )
    labels = qualify_source_labels(prepared.svg, raw_chart, raw_description)
    if labels.description != old.description:
        raise ValueError("Numeric promotion cannot rewrite the indexed description")
    proof_ref = assets.put(
        TypeAdapter(SourcePaintProof).dump_json(proof), media_type="application/json"
    )
    chart_ref = assets.put(
        TypeAdapter(ChartIR).dump_json(numeric.chart), media_type="application/json"
    )
    new_receipt = NumericLabelPublicationReceipt(
        object_id=receipt.object_id,
        source_manifest_id=receipt.source_manifest_id,
        region_id=receipt.region_id,
        ir=chart_ref,
        description=receipt.description,
        source_svg=receipt.source_svg,
        raw_chart=receipt.raw_chart,
        raw_description=receipt.raw_description,
        view=receipt.view,
        qualification=numeric.receipt,
        source_paint_proof=proof_ref,
    )
    result = replace(
        member,
        ir=chart_ref,
        qualification=assets.put(
            new_receipt.model_dump_json().encode(), media_type="application/json"
        ),
        lineage_refs=(*member.lineage_refs, proof_ref),
    )
    resolve_chart_member(sources, assets, scope, result)
    return result


def validate_chart_member(
    sources: LocalDocumentStore,
    assets: LocalDocumentStore,
    scope: ProcessingScope,
    member: RetrievalMember,
) -> tuple[ChartIR, TextDescription, FigureQualification]:
    resolved = resolve_chart_member(sources, assets, scope, member)
    return resolved.chart, resolved.description, resolved.qualification
