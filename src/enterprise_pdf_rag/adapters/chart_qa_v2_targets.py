"""Capture pins and provenance from a source-requalified immutable publication."""

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.bar_publication import parse_displayed_bar_receipt
from enterprise_pdf_rag.adapters.chart_qa_capture import CaptureTarget
from enterprise_pdf_rag.adapters.chart_qa_displayed import StoredDisplayResolver
from enterprise_pdf_rag.adapters.chart_qa_evaluation import QueryPinDto
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import (
    BarCaptureTarget,
    BarReleaseBindings,
    BindingDto,
    NormalizationDto,
)
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DescriptionNormalizationCitation,
)
from enterprise_pdf_rag.figures.chart_qa.models import QueryPin
from enterprise_pdf_rag.figures.models import SvgBinding


def source_qualified_capture_target(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    *,
    pin: QueryPin,
    endpoint: str,
) -> BarCaptureTarget:
    """No HTTP or answer construction: every expected ID comes from trusted closure."""
    http = CaptureTarget(
        endpoint=endpoint,
        processing_id=pin.processing_id,
        snapshot_id=pin.snapshot_id,
        member_id=pin.member_id,
    )
    context = StoredDisplayResolver(
        sources, outputs, processing_id=pin.processing_id
    ).resolve(pin)
    manifest = outputs.load(pin.processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    validate_processing_source(
        sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan
    )
    member = next(
        member for member in plan.members if member.member_id == pin.member_id
    )
    receipt = parse_displayed_bar_receipt(outputs.assets.get(member.qualification))
    period_methods = {period.confidence.method for period in context.point_periods}
    evidence_methods = {
        period.evidence.confidence.method for period in context.point_periods
    }
    context_methods = {item.confidence.method for item in context.page_context}
    if any(
        len(methods) != 1
        for methods in (period_methods, evidence_methods, context_methods)
    ):
        raise ValueError(
            "Capture requires one explicit confidence policy per evidence role"
        )
    return BarCaptureTarget(
        http=http,
        release=BarReleaseBindings(
            pin=QueryPinDto(
                processing_id=pin.processing_id,
                snapshot_id=pin.snapshot_id,
                member_id=pin.member_id,
            ),
            source_manifest_id=manifest.scope.source_manifest_id,
            binding=BindingDto.model_validate_json(
                TypeAdapter(SvgBinding).dump_json(context.svg.binding)
            ),
            raw_chart_ir_artifact_id=context.raw_chart.artifact_id,
            chart_ir_artifact_id=context.chart.artifact_id,
            qualification_id=context.qualification.artifact_id,
            publication_receipt_sha256=member.qualification.sha256,
            source_paint_proof_sha256=receipt.source_paint_proof.sha256,
            normalization=NormalizationDto.model_validate_json(
                TypeAdapter(DescriptionNormalizationCitation).dump_json(
                    context.description_normalization
                )
            ),
            period_confidence_method=next(iter(period_methods)),
            period_evidence_confidence_method=next(iter(evidence_methods)),
            page_context_confidence_method=next(iter(context_methods)),
        ),
    )
