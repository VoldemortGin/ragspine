"""Deterministic lookup and ordered percentage-point subtraction only."""

from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)

from ragspine.extraction.evidence.figures.chart_qa.evidence import (
    check_context,
    check_fields,
    citation,
    source_display,
)
from ragspine.extraction.evidence.figures.chart_qa.models import (
    AnswerValue,
    CalculationInput,
    CalculationReceipt,
    ChartAnswer,
    ChartContext,
    ChartQueryError,
    ChartQuestion,
    ChartRefusal,
    InputClaim,
    Operation,
    QueryFailure,
    QueryStatus,
    RefusalReason,
)
from ragspine.extraction.evidence.figures.chart_qa.ports import ChartContextResolver
from ragspine.extraction.evidence.figures.models import (
    ChartPoint,
    Confidence,
    FigureError,
    ValueKind,
    Verification,
)


def _abstain(question: ChartQuestion, reason: RefusalReason) -> ChartAnswer:
    return ChartAnswer(
        question.pin, question.operation, QueryStatus.ABSTAINED, None, (), None, reason
    )


def _claim(context: ChartContext, point: ChartPoint) -> InputClaim:
    assert context.chart.period is not None and point.value.value is not None
    fields = (
        (f"points.{point.point_id}.series", point.series.evidence),
        (f"points.{point.point_id}.category", point.category.evidence),
        ("period", context.chart.period.evidence),
        (f"points.{point.point_id}.unit", point.unit.evidence),
        (f"points.{point.point_id}.value", point.value.evidence),
    )
    return InputClaim(
        point.point_id,
        point.series.text,
        point.category.text,
        context.chart.period.text,
        point.unit.text,
        point.value.value,
        ValueKind.EXPLICIT,
        source_display(context, point),
        tuple(citation(context, path, evidence) for path, evidence in fields),
    )


def _precision_supported(value: Decimal) -> bool:
    exponent = value.as_tuple().exponent
    return (
        Decimal(0) <= value <= Decimal(100)
        and len(value.as_tuple().digits) <= 34
        and isinstance(exponent, int)
        and -28 <= exponent <= 2
    )


class ChartQAService:
    def __init__(self, resolver: ChartContextResolver) -> None:
        self.resolver = resolver

    def answer(self, question: ChartQuestion) -> ChartAnswer:
        try:
            context = self.resolver.resolve(question.pin)
            if context.pin != question.pin:
                raise ChartQueryError(
                    QueryFailure.PIN_CONFLICT,
                    "Resolver returned another immutable membership",
                )
            check_context(context)
            return self._answer(question, context)
        except ChartRefusal as error:
            return _abstain(question, error.reason)
        except FigureError as error:
            raise ChartQueryError(
                QueryFailure.INVALID_EVIDENCE,
                "Chart field/source evidence failed validation",
            ) from error

    def _answer(self, question: ChartQuestion, context: ChartContext) -> ChartAnswer:
        chart = context.chart
        if (
            context.qualification.semantic_scope != "explicit-distribution-shares"
            or chart.verification is not Verification.VERIFIED
            or context.description.verification is not Verification.VERIFIED
        ):
            return _abstain(question, RefusalReason.UNQUALIFIED_MEMBER)
        if chart.grammar != "donut":
            return _abstain(question, RefusalReason.UNSUPPORTED_GRAMMAR)
        if question.unit != "%":
            return _abstain(question, RefusalReason.UNIT_MISMATCH)
        if chart.period is None or chart.period.text != question.period:
            return _abstain(question, RefusalReason.PERIOD_MISMATCH)
        points: list[ChartPoint] = []
        for selector in question.points:
            point = next((p for p in chart.points if p.point_id == selector.point_id), None)
            if point is None:
                return _abstain(question, RefusalReason.UNKNOWN_POINT)
            for actual, expected, reason in (
                (point.series.text, question.series, RefusalReason.SERIES_MISMATCH),
                (
                    point.category.text,
                    selector.category,
                    RefusalReason.CATEGORY_MISMATCH,
                ),
                (point.unit.text, question.unit, RefusalReason.UNIT_MISMATCH),
            ):
                if actual != expected:
                    return _abstain(question, reason)
            if point.value.kind is not ValueKind.EXPLICIT or point.value.value is None:
                return _abstain(question, RefusalReason.UNSUPPORTED_VALUE_KIND)
            if not _precision_supported(point.value.value):
                return _abstain(question, RefusalReason.UNSUPPORTED_PRECISION)
            points.append(point)
        check_fields(context)
        claims = tuple(_claim(context, point) for point in points)
        if question.operation is Operation.LOOKUP:
            claim = claims[0]
            return ChartAnswer(
                question.pin,
                question.operation,
                QueryStatus.ANSWERED,
                AnswerValue(
                    claim.value,
                    "%",
                    ValueKind.EXPLICIT,
                    claim.raw_display,
                    Verification.VERIFIED,
                    Confidence(
                        None,
                        "source-qualified explicit percentage; deterministic field-to-occurrence verification",
                    ),
                ),
                claims,
                None,
                None,
            )
        policy = Context(prec=64, rounding="ROUND_HALF_EVEN", Emin=-999, Emax=999)
        for signal in (Inexact, Rounded, InvalidOperation, Overflow):
            policy.traps[signal] = True
        with localcontext(policy):
            value = claims[0].value - claims[1].value
        sources = tuple(
            field.occurrences[0].anchor
            for claim in claims
            for field in claim.citations
            if field.field_path == f"points.{claim.point_id}.value"
        )
        receipt = CalculationReceipt(
            question.pin,
            context.source_manifest_id,
            context.qualification.artifact_id,
            "percentage-point-difference-v1",
            question.operation,
            "base-10 exact subtraction; max 28 fractional places; trap Inexact and Rounded; no quantization",
            64,
            "ROUND_HALF_EVEN",
            tuple(
                CalculationInput(
                    context.chart.artifact_id,
                    f"points.{claim.point_id}.value",
                    claim.value,
                )
                for claim in claims
            ),
            value,
            "percentage_points",
            sources,
        )
        return ChartAnswer(
            question.pin,
            question.operation,
            QueryStatus.ANSWERED,
            AnswerValue(
                value,
                "percentage_points",
                ValueKind.DERIVED,
                None,
                Verification.VERIFIED,
                Confidence(
                    None,
                    "deterministic Decimal subtraction of source-qualified explicit percentages; not a model probability",
                ),
            ),
            claims,
            receipt,
            None,
        )
