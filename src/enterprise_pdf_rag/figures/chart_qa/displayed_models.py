"""Point-local period roles and scoped displayed-value query results."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from enterprise_pdf_rag.figures.chart_qa.models import (
    AnswerValue,
    FieldCitation,
    Operation,
    QueryPin,
    QueryStatus,
)
from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Confidence,
    Evidence,
    FigureQualification,
    SourceAnchor,
    SvgArtifact,
    SvgBinding,
    TextDescription,
    ValueKind,
    Verification,
    content_id,
)

PERIOD_RULE = "point-category-period-v1"
DISPLAYED_BAR_SCOPE = "displayed-percent-bar-lookup-v1"
type SemanticRole = Literal["series", "category", "period", "unit", "value"]


class DisplayedRefusalReason(StrEnum):
    UNQUALIFIED_MEMBER = "unqualified_member"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_GRAMMAR = "unsupported_grammar"
    UNKNOWN_POINT = "unknown_point"
    VALUE_UNAVAILABLE = "value_unavailable"
    SERIES_MISMATCH = "series_mismatch"
    CATEGORY_MISMATCH = "category_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    UNSUPPORTED_PRECISION = "unsupported_precision"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DisplayedRefusal(ValueError):
    def __init__(self, reason: DisplayedRefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PointPeriodInterpretation:
    binding: SvgBinding
    raw_chart_ir_artifact_id: str
    point_id: str
    raw_field_path: str
    literal: str
    evidence: Evidence
    rule_version: str
    verification: Verification
    confidence: Confidence

    @property
    def artifact_id(self) -> str:
        return content_id("point-period-interpretation-v1", (self,))


@dataclass(frozen=True, slots=True)
class PageContextCitation:
    source_manifest_id: str
    source_text_sha256: str
    source_span_id: str
    text: str
    source: SourceAnchor
    text_range: tuple[int, int]
    verification: Verification
    confidence: Confidence
    scope: Literal["page_context"] = "page_context"

    @property
    def artifact_id(self) -> str:
        return content_id("page-context-citation-v1", (self,))


@dataclass(frozen=True, slots=True)
class DescriptionNormalizationCitation:
    """Audit provenance only; repairing duplicate IDs does not qualify a fact."""

    binding: SvgBinding
    receipt_id: str
    raw_response_sha256: str
    original_typed_description_artifact_id: str
    normalized_description_artifact_id: str
    rule_version: str


@dataclass(frozen=True, slots=True)
class DisplayedLookupContext:
    """The trusted port rechecks source proof, normalization and raw lineage."""

    pin: QueryPin
    source_manifest_id: str
    raw_chart: ChartIR
    chart: ChartIR
    description: TextDescription
    qualification: FigureQualification
    svg: SvgArtifact
    point_periods: tuple[PointPeriodInterpretation, ...]
    page_context: tuple[PageContextCitation, ...]
    description_normalization: DescriptionNormalizationCitation


@dataclass(frozen=True, slots=True)
class ValidatedDisplayedBar:
    """Source-requalified values without authority over a query snapshot pin."""

    raw_chart: ChartIR
    chart: ChartIR
    description: TextDescription
    qualification: FigureQualification
    svg: SvgArtifact
    point_periods: tuple[PointPeriodInterpretation, ...]
    page_context: tuple[PageContextCitation, ...]
    description_normalization: DescriptionNormalizationCitation


@dataclass(frozen=True, slots=True)
class RoleFieldCitation:
    role: SemanticRole
    raw_chart_ir_artifact_id: str
    raw_field_path: str
    citation: FieldCitation


@dataclass(frozen=True, slots=True)
class DisplayedInputClaim:
    point_id: str
    series: str
    category: str
    period: str
    unit: str
    value: Decimal
    value_kind: ValueKind
    raw_display: str
    citations: tuple[RoleFieldCitation, ...]
    period_interpretation: PointPeriodInterpretation


@dataclass(frozen=True, slots=True)
class DisplayedAnswer:
    pin: QueryPin
    operation: Operation
    status: QueryStatus
    answer: AnswerValue | None
    inputs: tuple[DisplayedInputClaim, ...]
    page_context: tuple[PageContextCitation, ...]
    refusal_reason: DisplayedRefusalReason | None
    description_normalization: DescriptionNormalizationCitation | None = None
    semantic_scope: Literal["source_display_only"] = "source_display_only"
    calculation_receipt: None = None
