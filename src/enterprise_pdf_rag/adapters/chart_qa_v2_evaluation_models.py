"""Independent strict gold and observation DTOs for displayed bar lookup."""

from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from enterprise_pdf_rag.adapters.chart_qa_capture import CaptureTarget
from enterprise_pdf_rag.adapters.chart_qa_evaluation import (
    AnswerDto,
    CitationDto,
    ConfidenceDto,
    GoldCase,
    GoldCorpus,
    GoldFact,
    GoldFigure,
    GoldReview,
    QueryPinDto,
    SourceAnchorDto,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class BarGoldScope(StrictModel):
    grammar: Literal["bar"]
    series: str
    global_period: None
    unit: Literal["%"]
    allowed_operations: tuple[Literal["lookup"], ...]
    qualification_required: Literal["displayed-percent-bar-lookup-v1"]
    semantic_scope: Literal["source_display_only"]
    period_rule: Literal["point-category-period-v1"]


class BarGoldContext(StrictModel):
    source_span_id: str
    text: str
    bbox: tuple[float, float, float, float]
    text_range: tuple[int, int]


class BarGoldNormalization(StrictModel):
    rule_version: Literal["duplicate-evidence-normalization-v1"]
    raw_response_sha256: str
    original_typed_description_artifact_id: str


class BarThresholds(StrictModel):
    answer_precision: Literal["1"]
    positive_answer_coverage: Literal["1"]
    citation_exactness: Literal["1"]
    citation_coverage: Literal["1"]
    period_role_exactness: Literal["1"]
    page_context_exactness: Literal["1"]
    normalization_provenance_exactness: Literal["1"]
    hard_negative_escape_rate: Literal["0"]


class BarGold(StrictModel):
    schema_version: Literal["chart-qa-bar-gold-v1"]
    source_manifest_id: str
    corpus: GoldCorpus
    scope: BarGoldScope
    figure: GoldFigure
    review: GoldReview
    facts: tuple[GoldFact, ...]
    page_context: BarGoldContext
    normalization: BarGoldNormalization
    cases: tuple[GoldCase, ...]
    thresholds: BarThresholds
    minimum_positive_cases: Literal[2]
    adversarial_response_mutations: tuple[str, ...]

    @model_validator(mode="after")
    def validate_gold(self) -> "BarGold":
        if any(
            not is_digest(digest)
            for digest in (
                self.source_manifest_id,
                self.corpus.document_sha256,
                self.figure.page_text_sidecar_sha256,
                self.figure.region_source_text_sha256,
                self.figure.native_crop_svg_sha256,
                self.figure.structured_svg_sha256,
                self.figure.source_render_sha256,
                self.normalization.raw_response_sha256,
            )
        ):
            raise ValueError("Gold source identities require SHA-256 digests")
        if (
            self.figure.page_index + 1 != self.figure.physical_page_number
            or self.corpus.selected_physical_pages != tuple(range(1, 21))
            or self.scope.allowed_operations != ("lookup",)
            or self.page_context.text_range != (0, len(self.page_context.text))
        ):
            raise ValueError("Gold page, lookup scope or context range is invalid")
        facts = {fact.point_id: fact for fact in self.facts}
        if len(facts) != len(self.facts) or len({c.case_id for c in self.cases}) != len(
            self.cases
        ):
            raise ValueError("Gold fact and case IDs must be unique")
        positives = tuple(c for c in self.cases if c.case_class == "positive")
        if len(positives) < self.minimum_positive_cases:
            raise ValueError("Gold must require at least two positive lookups")
        for fact in self.facts:
            if (
                fact.evidence.period != fact.evidence.category
                or fact.category != fact.evidence.category.text
                or fact.evidence.series.text != self.scope.series
                or fact.evidence.unit.text != "%"
                or fact.evidence.value.text != fact.value
                or fact.raw_display != fact.value + "%"
                or not Decimal(fact.value).is_finite()
            ):
                raise ValueError(
                    "Gold positive fact differs from its source occurrence"
                )
        for case in self.cases:
            expected = case.expected
            if case.case_class != "positive":
                if expected.business_status == "answered":
                    raise ValueError("Negative gold cannot expect an answer")
                continue
            if len(expected.fact_ids) != 1 or expected.fact_ids[0] not in facts:
                raise ValueError("Gold positive must select exactly one known fact")
            fact = facts[expected.fact_ids[0]]
            request = case.request
            if (
                expected.business_status != "answered"
                or expected.http_status != 200
                or expected.value != fact.value
                or expected.unit != "%"
                or expected.value_kind != "explicit"
                or expected.raw_display != fact.raw_display
                or expected.verification != "verified"
                or expected.confidence_score is not None
                or "confidence_score" not in expected.model_fields_set
                or not expected.confidence_method
                or request.operation != "lookup"
                or request.series != self.scope.series
                or request.period != fact.category
                or request.unit != "%"
                or len(request.points) != 1
                or request.points[0].point_id != fact.point_id
                or request.points[0].category != fact.category
            ):
                raise ValueError(
                    "Gold positive expected result differs from source fact"
                )
        return self


def load_bar_gold(payload: bytes) -> BarGold:
    return BarGold.model_validate_json(payload, strict=True, extra="forbid")


class BindingDto(StrictModel):
    figure_id: str
    source_revision: str
    svg_artifact_id: str
    svg_digest: str


class EvidenceDto(StrictModel):
    element_ids: tuple[str, ...]
    verification: str
    confidence: ConfidenceDto


class PeriodDto(StrictModel):
    binding: BindingDto
    raw_chart_ir_artifact_id: str
    point_id: str
    raw_field_path: str
    literal: str
    evidence: EvidenceDto
    rule_version: str
    verification: str
    confidence: ConfidenceDto


class RoleCitationDto(StrictModel):
    role: str
    raw_chart_ir_artifact_id: str
    raw_field_path: str
    citation: CitationDto


class DisplayedInputDto(StrictModel):
    point_id: str
    series: str
    category: str
    period: str
    unit: str
    value: str
    value_kind: str
    raw_display: str
    citations: tuple[RoleCitationDto, ...]
    period_interpretation: PeriodDto


class ContextDto(StrictModel):
    source_manifest_id: str
    source_text_sha256: str
    source_span_id: str
    text: str
    source: SourceAnchorDto
    text_range: tuple[int, int]
    verification: str
    confidence: ConfidenceDto
    scope: str


class NormalizationDto(StrictModel):
    binding: BindingDto
    receipt_id: str
    raw_response_sha256: str
    original_typed_description_artifact_id: str
    normalized_description_artifact_id: str
    rule_version: str


class BarResponseDto(StrictModel):
    schema_version: Literal["chart-qa-v2"]
    semantic_scope: str
    processing_id: str
    snapshot_id: str
    member_id: str
    operation: str
    status: Literal["answered", "abstained"]
    answer: AnswerDto | None
    inputs: tuple[DisplayedInputDto, ...]
    page_context: tuple[ContextDto, ...]
    description_normalization: NormalizationDto | None
    calculation_receipt: None
    refusal_reason: str | None


class BarReleaseBindings(StrictModel):
    """Expected identities obtained from source-requalified publication, never HTTP."""

    pin: QueryPinDto
    source_manifest_id: str
    binding: BindingDto
    raw_chart_ir_artifact_id: str
    chart_ir_artifact_id: str
    qualification_id: str
    publication_receipt_sha256: str
    source_paint_proof_sha256: str
    normalization: NormalizationDto
    period_confidence_method: str
    period_evidence_confidence_method: str
    page_context_confidence_method: str

    @model_validator(mode="after")
    def validate_identities(self) -> "BarReleaseBindings":
        if not all(
            is_digest(value)
            for value in (
                self.pin.processing_id,
                self.pin.snapshot_id,
                self.pin.member_id,
                self.source_manifest_id,
                self.binding.source_revision,
                self.binding.svg_digest,
                self.publication_receipt_sha256,
                self.source_paint_proof_sha256,
                self.normalization.raw_response_sha256,
            )
        ):
            raise ValueError("Publication closure requires complete SHA-256 identities")
        for value, prefix in (
            (self.raw_chart_ir_artifact_id, "chart-v2"),
            (self.chart_ir_artifact_id, "chart-v2"),
            (self.qualification_id, "qualification-v1"),
            (self.binding.figure_id, "figure-v1"),
            (self.binding.svg_artifact_id, "svg-v2"),
            (self.normalization.receipt_id, "description-normalization-v1"),
            (
                self.normalization.original_typed_description_artifact_id,
                "description-v2",
            ),
            (self.normalization.normalized_description_artifact_id, "description-v2"),
        ):
            if not value.startswith(prefix + ":") or not is_digest(
                value[len(prefix) + 1 :]
            ):
                raise ValueError("Publication closure contains an invalid artifact ID")
        if (
            self.normalization.binding != self.binding
            or self.normalization.rule_version != "duplicate-evidence-normalization-v1"
            or self.normalization.original_typed_description_artifact_id
            == self.normalization.normalized_description_artifact_id
            or self.raw_chart_ir_artifact_id == self.chart_ir_artifact_id
            or not all(
                method.strip()
                for method in (
                    self.period_confidence_method,
                    self.period_evidence_confidence_method,
                    self.page_context_confidence_method,
                )
            )
        ):
            raise ValueError("Publication closure normalization/projection differs")
        return self


class BarCaptureTarget(StrictModel):
    http: CaptureTarget
    release: BarReleaseBindings | None

    @model_validator(mode="after")
    def validate_pin(self) -> "BarCaptureTarget":
        if self.release is not None and self.release.pin != QueryPinDto(
            processing_id=self.http.processing_id,
            snapshot_id=self.http.snapshot_id,
            member_id=self.http.member_id,
        ):
            raise ValueError(
                "Capture pin differs from independently checked publication"
            )
        return self


class BarCaptureTargets(StrictModel):
    schema_version: Literal["chart-qa-v2-capture-targets-v1"]
    targets: dict[str, BarCaptureTarget]


class BarObservedCase(StrictModel):
    case_id: str
    http_status: int
    request_json: str
    response_json: str
    response_sha256: str

    @model_validator(mode="after")
    def validate_raw_response(self) -> "BarObservedCase":
        if sha256(self.response_json.encode()).hexdigest() != self.response_sha256:
            raise ValueError("Raw HTTP response digest differs")
        return self


class BarObservations(StrictModel):
    schema_version: Literal["chart-qa-v2-observations-v1"]
    targets_sha256: str
    results: tuple[BarObservedCase, ...]
