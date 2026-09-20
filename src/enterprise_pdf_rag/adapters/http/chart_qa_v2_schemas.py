"""Explicit lookup-only bar API; legacy donut request/response stay unchanged."""

from typing import Literal

from pydantic import Field

from enterprise_pdf_rag.adapters.http.chart_qa_schemas import PointSelectorInput
from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DescriptionNormalizationCitation,
    DisplayedAnswer,
    DisplayedInputClaim,
    DisplayedRefusalReason,
    PageContextCitation,
)
from enterprise_pdf_rag.figures.chart_qa.models import (
    AnswerValue,
    ChartQuestion,
    Operation,
    PointSelector,
    QueryPin,
    QueryStatus,
)


class DisplayedChartQueryRequest(BoundaryModel):
    schema_version: Literal["chart-qa-v2"]
    kind: Literal["chart"]
    processing_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["lookup"]
    series: str = Field(min_length=1, max_length=256)
    period: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=32)
    points: list[PointSelectorInput] = Field(min_length=1, max_length=1)

    def to_domain(self) -> ChartQuestion:
        return ChartQuestion(
            QueryPin(self.processing_id, self.snapshot_id, self.member_id),
            Operation.LOOKUP,
            self.series,
            self.period,
            self.unit,
            tuple(PointSelector(item.point_id, item.category) for item in self.points),
        )


class DisplayedChartQueryResponse(BoundaryModel):
    schema_version: Literal["chart-qa-v2"] = "chart-qa-v2"
    semantic_scope: Literal["source_display_only"] = "source_display_only"
    processing_id: str
    snapshot_id: str
    member_id: str
    operation: Literal["lookup"] = "lookup"
    status: QueryStatus
    answer: AnswerValue | None
    inputs: tuple[DisplayedInputClaim, ...]
    page_context: tuple[PageContextCitation, ...]
    description_normalization: DescriptionNormalizationCitation | None
    calculation_receipt: None = None
    refusal_reason: DisplayedRefusalReason | None

    @classmethod
    def from_domain(cls, result: DisplayedAnswer) -> "DisplayedChartQueryResponse":
        if result.operation is not Operation.LOOKUP:
            raise ValueError("ChartQA v2 admits only explicit lookup requests")
        return cls(
            processing_id=result.pin.processing_id,
            snapshot_id=result.pin.snapshot_id,
            member_id=result.pin.member_id,
            status=result.status,
            answer=result.answer,
            inputs=result.inputs,
            page_context=result.page_context,
            description_normalization=result.description_normalization,
            refusal_reason=result.refusal_reason,
        )
