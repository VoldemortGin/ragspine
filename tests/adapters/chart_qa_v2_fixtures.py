"""Authored HTTP observations for falsifying the independent v2 evaluator."""

import json
from hashlib import sha256

from enterprise_pdf_rag.adapters.chart_qa_capture import CaptureTarget
from enterprise_pdf_rag.adapters.chart_qa_evaluation import (
    AnswerDto,
    CitationDto,
    ConfidenceDto,
    GoldCase,
    GoldOccurrence,
    OccurrenceDto,
    QueryPinDto,
    SourceAnchorDto,
)
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import (
    BarCaptureTarget,
    BarCaptureTargets,
    BarGold,
    BarObservations,
    BarObservedCase,
    BarReleaseBindings,
    BarResponseDto,
    BindingDto,
    ContextDto,
    DisplayedInputDto,
    EvidenceDto,
    NormalizationDto,
    PeriodDto,
    RoleCitationDto,
)


def targets_fixture(gold: BarGold) -> BarCaptureTargets:
    binding = BindingDto(
        figure_id="figure-v1:" + "4" * 64,
        source_revision=gold.corpus.document_sha256,
        svg_artifact_id="svg-v2:" + "5" * 64,
        svg_digest=gold.figure.structured_svg_sha256,
    )
    release = BarReleaseBindings(
        pin=QueryPinDto(
            processing_id="1" * 64, snapshot_id="2" * 64, member_id="3" * 64
        ),
        source_manifest_id=gold.source_manifest_id,
        binding=binding,
        raw_chart_ir_artifact_id="chart-v2:" + "6" * 64,
        chart_ir_artifact_id="chart-v2:" + "7" * 64,
        qualification_id="qualification-v1:" + "8" * 64,
        publication_receipt_sha256="9" * 64,
        source_paint_proof_sha256="a" * 64,
        period_confidence_method="source-qualified explicit occurrence",
        period_evidence_confidence_method="source-qualified explicit occurrence",
        page_context_confidence_method="source-qualified explicit occurrence",
        normalization=NormalizationDto(
            binding=binding,
            receipt_id="description-normalization-v1:" + "b" * 64,
            raw_response_sha256=gold.normalization.raw_response_sha256,
            original_typed_description_artifact_id=gold.normalization.original_typed_description_artifact_id,
            normalized_description_artifact_id="description-v2:" + "c" * 64,
            rule_version=gold.normalization.rule_version,
        ),
    )
    return BarCaptureTargets(
        schema_version="chart-qa-v2-capture-targets-v1",
        targets={
            name: BarCaptureTarget(
                http=CaptureTarget(
                    endpoint="http://127.0.0.1:18766",
                    processing_id="1" * 64,
                    snapshot_id="2" * 64,
                    member_id="3" * 64,
                ),
                release=release if name == "qualified" else None,
            )
            for name in {case.request.target for case in gold.cases}
        },
    )


def anchor(gold: BarGold, bbox: tuple[float, float, float, float]) -> SourceAnchorDto:
    return SourceAnchorDto(
        source_revision=gold.corpus.document_sha256,
        document_sha256=gold.corpus.document_sha256,
        page_index=gold.figure.page_index,
        bbox=bbox,
        coordinate_frame=gold.figure.coordinate_frame,
        rotation=0,
        transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    )


def request_json(case: GoldCase, target: BarCaptureTarget) -> str:
    return json.dumps(
        {
            "schema_version": "chart-qa-v2",
            "kind": "chart",
            "processing_id": target.http.processing_id,
            "snapshot_id": target.http.snapshot_id,
            "member_id": target.http.member_id,
            **case.request.model_dump(exclude={"target"}),
        },
        separators=(",", ":"),
    )


def response_fixture(
    gold: BarGold, case: GoldCase, target: BarCaptureTarget
) -> BarResponseDto:
    body = BarResponseDto(
        schema_version="chart-qa-v2",
        semantic_scope="source_display_only",
        processing_id=target.http.processing_id,
        snapshot_id=target.http.snapshot_id,
        member_id=target.http.member_id,
        operation="lookup",
        status="abstained",
        answer=None,
        inputs=(),
        page_context=(),
        description_normalization=None,
        calculation_receipt=None,
        refusal_reason=case.expected.reason_class,
    )
    if case.expected.business_status != "answered":
        return body
    release = target.release
    assert release is not None
    fact = next(
        fact for fact in gold.facts if fact.point_id == case.expected.fact_ids[0]
    )
    confidence = ConfidenceDto(
        score=None, method="source-qualified explicit occurrence"
    )
    fields: list[RoleCitationDto] = []
    for role in ("series", "category", "period", "unit", "value"):
        occurrence: GoldOccurrence = getattr(fact.evidence, role)
        path = f"points.{fact.point_id}.{'category' if role == 'period' else role}"
        fields.append(
            RoleCitationDto(
                role=role,
                raw_chart_ir_artifact_id=release.raw_chart_ir_artifact_id,
                raw_field_path=path,
                citation=CitationDto(
                    field_path=path,
                    chart_ir_artifact_id=release.chart_ir_artifact_id,
                    svg_artifact_id=release.binding.svg_artifact_id,
                    svg_digest=release.binding.svg_digest,
                    qualification_id=release.qualification_id,
                    occurrences=(
                        OccurrenceDto(
                            element_id=occurrence.observation_id,
                            text=occurrence.text,
                            anchor=anchor(gold, occurrence.bbox),
                            evidence_kind="source_text_observation",
                            source_span_id=occurrence.source_span_id,
                            text_range=(occurrence.start, occurrence.end),
                        ),
                    ),
                ),
            )
        )
    claim = DisplayedInputDto(
        point_id=fact.point_id,
        series=gold.scope.series,
        category=fact.category,
        period=fact.category,
        unit="%",
        value=fact.value,
        value_kind="explicit",
        raw_display=fact.raw_display,
        citations=tuple(fields),
        period_interpretation=PeriodDto(
            binding=release.binding,
            raw_chart_ir_artifact_id=release.raw_chart_ir_artifact_id,
            point_id=fact.point_id,
            raw_field_path=f"points.{fact.point_id}.category",
            literal=fact.category,
            evidence=EvidenceDto(
                element_ids=(fact.evidence.category.observation_id,),
                verification="verified",
                confidence=confidence,
            ),
            rule_version=gold.scope.period_rule,
            verification="verified",
            confidence=confidence,
        ),
    )
    return body.model_copy(
        update={
            "status": "answered",
            "refusal_reason": None,
            "answer": AnswerDto(
                value=fact.value,
                unit="%",
                value_kind="explicit",
                raw_display=fact.raw_display,
                verification="verified",
                confidence=ConfidenceDto(
                    score=None, method=case.expected.confidence_method or ""
                ),
            ),
            "inputs": (claim,),
            "page_context": (
                ContextDto(
                    source_manifest_id=gold.source_manifest_id,
                    source_text_sha256=gold.figure.page_text_sidecar_sha256,
                    source_span_id=gold.page_context.source_span_id,
                    text=gold.page_context.text,
                    source=anchor(gold, gold.page_context.bbox),
                    text_range=gold.page_context.text_range,
                    verification="verified",
                    confidence=confidence,
                    scope="page_context",
                ),
            ),
            "description_normalization": release.normalization,
        }
    )


def observations_fixture(gold: BarGold, targets: BarCaptureTargets) -> BarObservations:
    results = []
    for case in gold.cases:
        target = targets.targets[case.request.target]
        response = (
            response_fixture(gold, case, target).model_dump_json()
            if case.expected.http_status == 200
            else json.dumps({"detail": "unsupported request"})
            if case.expected.http_status == 422
            else json.dumps(
                {
                    "error": {
                        "code": case.expected.reason_class,
                        "message": "fixture rejection",
                    }
                }
            )
        )
        results.append(
            BarObservedCase(
                case_id=case.case_id,
                http_status=case.expected.http_status,
                request_json=request_json(case, target),
                response_json=response,
                response_sha256=sha256(response.encode()).hexdigest(),
            )
        )
    return BarObservations(
        schema_version="chart-qa-v2-observations-v1",
        targets_sha256=sha256(targets.model_dump_json().encode()).hexdigest(),
        results=tuple(results),
    )
