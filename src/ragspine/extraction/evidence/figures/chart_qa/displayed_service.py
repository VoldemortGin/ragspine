"""Read explicit bar labels through a distinct lookup-only trusted port."""

from ragspine.extraction.evidence.figures.chart_qa.displayed_evidence import (
    chart_context,
    check_displayed_evidence,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import (
    DISPLAYED_BAR_SCOPE,
    DisplayedAnswer,
    DisplayedInputClaim,
    DisplayedLookupContext,
    DisplayedRefusal,
    DisplayedRefusalReason,
    RoleFieldCitation,
    SemanticRole,
)
from ragspine.extraction.evidence.figures.chart_qa.displayed_ports import DisplayedContextResolver
from ragspine.extraction.evidence.figures.chart_qa.evidence import citation, source_display
from ragspine.extraction.evidence.figures.chart_qa.models import (
    AnswerValue,
    ChartQueryError,
    ChartQuestion,
    Operation,
    QueryFailure,
    QueryStatus,
)
from ragspine.extraction.evidence.figures.models import (
    Confidence,
    Evidence,
    FigureError,
    ValueKind,
    Verification,
)


def abstain(question: ChartQuestion, reason: DisplayedRefusalReason) -> DisplayedAnswer:
    return DisplayedAnswer(
        question.pin, question.operation, QueryStatus.ABSTAINED, None, (), (), reason
    )


class DisplayedChartQAService:
    def __init__(self, resolver: DisplayedContextResolver) -> None:
        self.resolver = resolver

    def answer(self, question: ChartQuestion) -> DisplayedAnswer:
        if question.operation is not Operation.LOOKUP:
            return abstain(question, DisplayedRefusalReason.UNSUPPORTED_OPERATION)
        try:
            context = self.resolver.resolve(question.pin)
            if context.pin != question.pin:
                raise ChartQueryError(
                    QueryFailure.PIN_CONFLICT,
                    "Resolver returned another immutable membership",
                )
            return self._answer(question, context)
        except DisplayedRefusal as error:
            return abstain(question, error.reason)
        except FigureError as error:
            raise ChartQueryError(
                QueryFailure.INVALID_EVIDENCE,
                "Displayed source evidence failed validation",
            ) from error

    def _answer(self, question: ChartQuestion, context: DisplayedLookupContext) -> DisplayedAnswer:
        if context.qualification.semantic_scope != DISPLAYED_BAR_SCOPE:
            return abstain(question, DisplayedRefusalReason.UNQUALIFIED_MEMBER)
        if context.chart.grammar != "bar" or context.raw_chart.grammar != "bar":
            return abstain(question, DisplayedRefusalReason.UNSUPPORTED_GRAMMAR)
        evidence_context = chart_context(context)
        check_displayed_evidence(context)
        selector = question.points[0]
        raw = next(
            (p for p in context.raw_chart.points if p.point_id == selector.point_id),
            None,
        )
        if raw is None:
            return abstain(question, DisplayedRefusalReason.UNKNOWN_POINT)
        for actual, expected, reason in (
            (raw.series.text, question.series, DisplayedRefusalReason.SERIES_MISMATCH),
            (
                raw.category.text,
                selector.category,
                DisplayedRefusalReason.CATEGORY_MISMATCH,
            ),
            (
                raw.category.text,
                question.period,
                DisplayedRefusalReason.PERIOD_MISMATCH,
            ),
            ("%", question.unit, DisplayedRefusalReason.UNIT_MISMATCH),
        ):
            if actual != expected:
                return abstain(question, reason)
        if raw.value.kind is ValueKind.UNAVAILABLE:
            return abstain(question, DisplayedRefusalReason.VALUE_UNAVAILABLE)
        point = next((p for p in context.chart.points if p.point_id == selector.point_id), None)
        if point is None:
            return abstain(question, DisplayedRefusalReason.INSUFFICIENT_EVIDENCE)
        period = next(p for p in context.point_periods if p.point_id == selector.point_id)
        assert point.value.value is not None
        role_fields: tuple[tuple[SemanticRole, str, Evidence], ...] = (
            ("series", "series", point.series.evidence),
            ("category", "category", point.category.evidence),
            ("period", "category", period.evidence),
            ("unit", "unit", point.unit.evidence),
            ("value", "value", point.value.evidence),
        )
        fields = (
            RoleFieldCitation(
                role,
                context.raw_chart.artifact_id,
                f"points.{point.point_id}.{name}",
                citation(evidence_context, f"points.{point.point_id}.{name}", evidence),
            )
            for role, name, evidence in role_fields
        )
        claim = DisplayedInputClaim(
            point.point_id,
            point.series.text,
            point.category.text,
            period.literal,
            point.unit.text,
            point.value.value,
            ValueKind.EXPLICIT,
            source_display(evidence_context, point),
            tuple(fields),
            period,
        )
        return DisplayedAnswer(
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
                    "source-qualified explicit display; no cross-period calculation",
                ),
            ),
            (claim,),
            context.page_context,
            None,
            context.description_normalization,
        )
