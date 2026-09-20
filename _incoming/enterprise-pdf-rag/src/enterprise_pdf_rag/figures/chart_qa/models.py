"""Immutable questions, cited source claims and separate calculation receipts."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from enterprise_pdf_rag.figures.models import (
    ChartIR,
    Confidence,
    FigureQualification,
    SourceAnchor,
    SvgArtifact,
    SvgElement,
    TextDescription,
    ValueKind,
    Verification,
    content_id,
)


class Operation(StrEnum):
    LOOKUP = "lookup"
    PERCENTAGE_POINT_DIFFERENCE = "percentage_point_difference"


class QueryStatus(StrEnum):
    ANSWERED = "answered"
    ABSTAINED = "abstained"


class RefusalReason(StrEnum):
    UNQUALIFIED_MEMBER = "unqualified_member"
    UNSUPPORTED_GRAMMAR = "unsupported_grammar"
    UNKNOWN_POINT = "unknown_point"
    SERIES_MISMATCH = "series_mismatch"
    CATEGORY_MISMATCH = "category_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    UNSUPPORTED_PRECISION = "unsupported_precision"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class QueryFailure(StrEnum):
    PIN_CONFLICT = "pin_conflict"
    INVALID_EVIDENCE = "invalid_evidence"
    UNAVAILABLE_EVIDENCE = "unavailable_evidence"


class ChartQueryError(ValueError):
    def __init__(self, code: QueryFailure, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChartRefusal(ValueError):
    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class QueryPin:
    processing_id: str
    snapshot_id: str
    member_id: str

    def __post_init__(self) -> None:
        for value in (self.processing_id, self.snapshot_id, self.member_id):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Query pins require lowercase SHA-256 identities")


@dataclass(frozen=True, slots=True)
class PointSelector:
    point_id: str
    category: str


@dataclass(frozen=True, slots=True)
class ChartQuestion:
    pin: QueryPin
    operation: Operation
    series: str
    period: str
    unit: str
    points: tuple[PointSelector, ...]

    def __post_init__(self) -> None:
        required = 1 if self.operation is Operation.LOOKUP else 2
        if not isinstance(self.points, tuple) or len(self.points) != required:
            raise ValueError("The operation requires one or two ordered points")
        if not all((self.series, self.period, self.unit)) or any(
            not p.point_id or not p.category for p in self.points
        ):
            raise ValueError("Every requested semantic field must be explicit")


@dataclass(frozen=True, slots=True)
class ChartContext:
    """A resolver-requalified member, never a fabricated FigureBundle."""

    pin: QueryPin
    source_manifest_id: str
    chart: ChartIR
    description: TextDescription
    qualification: FigureQualification
    svg: SvgArtifact


@dataclass(frozen=True, slots=True)
class FieldCitation:
    field_path: str
    chart_ir_artifact_id: str
    svg_artifact_id: str
    svg_digest: str
    qualification_id: str
    occurrences: tuple[SvgElement, ...]


@dataclass(frozen=True, slots=True)
class InputClaim:
    point_id: str
    series: str
    category: str
    period: str
    unit: str
    value: Decimal
    value_kind: ValueKind
    raw_display: str
    citations: tuple[FieldCitation, ...]


@dataclass(frozen=True, slots=True)
class AnswerValue:
    value: Decimal
    unit: str
    value_kind: ValueKind
    raw_display: str | None
    verification: Verification
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class CalculationInput:
    chart_ir_artifact_id: str
    field_path: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class CalculationReceipt:
    pin: QueryPin
    source_manifest_id: str
    qualification_id: str
    rule_version: str
    operation: Operation
    decimal_policy: str
    precision: int
    rounding: str
    inputs: tuple[CalculationInput, ...]
    output_value: Decimal
    output_unit: str
    source_refs: tuple[SourceAnchor, ...]

    @property
    def artifact_id(self) -> str:
        return content_id("chart-calculation-v1", (self,))


@dataclass(frozen=True, slots=True)
class ChartAnswer:
    pin: QueryPin
    operation: Operation
    status: QueryStatus
    answer: AnswerValue | None
    inputs: tuple[InputClaim, ...]
    calculation_receipt: CalculationReceipt | None
    refusal_reason: RefusalReason | None
