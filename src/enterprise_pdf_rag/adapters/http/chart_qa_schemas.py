"""Strict ChartQA v1 input and immutable, source-cited output contracts."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from ragspine.extraction.evidence.figures.chart_qa.models import (
    AnswerValue,
    CalculationInput,
    CalculationReceipt,
    ChartAnswer,
    ChartQuestion,
    InputClaim,
    Operation,
    PointSelector,
    QueryPin,
    QueryStatus,
    RefusalReason,
)
from ragspine.extraction.evidence.figures.models import SourceAnchor


class PointSelectorInput(BoundaryModel):
    point_id: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=256)


class ChartQueryRequest(BoundaryModel):
    kind: Literal["chart"]
    processing_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["lookup", "percentage_point_difference"]
    series: str = Field(min_length=1, max_length=256)
    period: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=32)
    points: list[PointSelectorInput] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def cardinality(self) -> "ChartQueryRequest":
        required = 1 if self.operation == "lookup" else 2
        if len(self.points) != required:
            raise ValueError("Operation requires one or two ordered operands")
        return self

    def to_domain(self) -> ChartQuestion:
        return ChartQuestion(
            QueryPin(self.processing_id, self.snapshot_id, self.member_id),
            Operation(self.operation),
            self.series,
            self.period,
            self.unit,
            tuple(PointSelector(p.point_id, p.category) for p in self.points),
        )


class CalculationReceiptResponse(BoundaryModel):
    receipt_id: str
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

    @classmethod
    def from_domain(cls, receipt: CalculationReceipt) -> "CalculationReceiptResponse":
        return cls(
            receipt_id=receipt.artifact_id,
            pin=receipt.pin,
            source_manifest_id=receipt.source_manifest_id,
            qualification_id=receipt.qualification_id,
            rule_version=receipt.rule_version,
            operation=receipt.operation,
            decimal_policy=receipt.decimal_policy,
            precision=receipt.precision,
            rounding=receipt.rounding,
            inputs=receipt.inputs,
            output_value=receipt.output_value,
            output_unit=receipt.output_unit,
            source_refs=receipt.source_refs,
        )


class ChartQueryResponse(BoundaryModel):
    schema_version: Literal["chart-qa-v1"] = "chart-qa-v1"
    processing_id: str
    snapshot_id: str
    member_id: str
    operation: Operation
    status: QueryStatus
    answer: AnswerValue | None
    inputs: tuple[InputClaim, ...]
    calculation_receipt: CalculationReceiptResponse | None
    refusal_reason: RefusalReason | None

    @classmethod
    def from_domain(cls, result: ChartAnswer) -> "ChartQueryResponse":
        return cls(
            processing_id=result.pin.processing_id,
            snapshot_id=result.pin.snapshot_id,
            member_id=result.pin.member_id,
            operation=result.operation,
            status=result.status,
            answer=result.answer,
            inputs=result.inputs,
            calculation_receipt=None
            if result.calculation_receipt is None
            else CalculationReceiptResponse.from_domain(result.calculation_receipt),
            refusal_reason=result.refusal_reason,
        )


class ChartQueryErrorDetail(BoundaryModel):
    code: Literal["pin_conflict", "invalid_evidence", "unavailable_evidence"]
    message: str


class ChartQueryErrorResponse(BoundaryModel):
    error: ChartQueryErrorDetail
