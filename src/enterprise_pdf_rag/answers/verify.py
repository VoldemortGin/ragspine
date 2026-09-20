"""Re-read every model claim from stored evidence, then gate the prose on verified numbers.

Nothing here derives a value: a quote must be a verbatim substring of its span, a
cell must equal its stored text, and a chart value must equal both the qualified
``Decimal`` and the original SVG display. Donut members go through
``check_context``/``check_fields``; displayed bars through
``check_displayed_evidence`` and the ``chart_context`` projection.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from enterprise_pdf_rag.answers.models import (
    AbstainReason,
    AnswerStatus,
    ClaimCitation,
    ClaimKind,
    RejectedClaim,
    VerifiedClaim,
    from_refusal,
)
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.figures.chart_qa.displayed_evidence import (
    chart_context,
    check_displayed_evidence,
)
from enterprise_pdf_rag.figures.chart_qa.displayed_models import (
    DISPLAYED_BAR_SCOPE,
    DisplayedLookupContext,
    DisplayedRefusal,
)
from enterprise_pdf_rag.figures.chart_qa.evidence import (
    check_context,
    check_fields,
    citation,
    source_display,
)
from enterprise_pdf_rag.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    ChartRefusal,
    FieldCitation,
    QueryFailure,
)
from enterprise_pdf_rag.figures.chart_qa.service import _precision_supported
from enterprise_pdf_rag.figures.models import (
    ChartPoint,
    Evidence,
    FigureError,
    ValueKind,
    Verification,
)
from enterprise_pdf_rag.processing.context_builder import BlockKind, ContextBlock
from enterprise_pdf_rag.processing.table_models import CellContentState

type ChartEvidence = Callable[[str], ChartContext | DisplayedLookupContext]

_DONUT_SCOPE = "explicit-distribution-shares"
_PATH_PREFIX = {
    ClaimKind.QUOTE: "fragments.",
    ClaimKind.CELL: "cells.",
    ClaimKind.CHART_VALUE: "points.",
}
_BLOCK_KINDS = {
    ClaimKind.QUOTE: {BlockKind.TEXT, BlockKind.LIST, BlockKind.GROUP},
    ClaimKind.CELL: {BlockKind.TABLE},
    ClaimKind.CHART_VALUE: {BlockKind.CHART},
}
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*%?(?![\w%])")
_POINT_VALUE_RE = re.compile(r"points\.(?P<point>[^.]+)\.value")


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    verified: tuple[VerifiedClaim, ...]
    rejected: tuple[RejectedClaim, ...]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").replace("%", "").strip())
    except InvalidOperation:
        return None


def _numbers(text: str) -> tuple[tuple[str, Decimal], ...]:
    found = []
    for match in _NUMBER_RE.finditer(text):
        value = _decimal(match.group())
        if value is not None:
            found.append((match.group().strip(), value))
    return tuple(found)


def _reject(claim: ModelClaim, reason: AbstainReason, detail: str) -> RejectedClaim:
    return RejectedClaim(
        claim.claim_id, claim.member_id, claim.field_path, claim.text, reason, detail
    )


def _verify_quote(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    span_id = claim.field_path.removeprefix("fragments.")
    span = next((item for item in block.spans if item.source_span_id == span_id), None)
    if span is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited span is not in the block")
    wanted = _norm(claim.text)
    if not wanted or wanted not in _norm(span.text):
        return _reject(
            claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text is not a verbatim substring"
        )
    return VerifiedClaim(
        claim.claim_id,
        ClaimKind.QUOTE,
        claim.text,
        None,
        None,
        (
            ClaimCitation(
                block.member_id,
                block.kind,
                span.page_index,
                claim.field_path,
                (span.source_span_id,),
                span.bbox,
                span.text,
            ),
        ),
    )


def _verify_cell(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    cell_id = claim.field_path.removeprefix("cells.")
    cell = next((item for item in block.cells if item.cell_id == cell_id), None)
    if cell is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited cell is not in the block")
    if cell.content_state is not CellContentState.PRESENT or cell.text is None:
        return _reject(
            claim, AbstainReason.VALUE_UNAVAILABLE, f"cell content is {cell.content_state.value}"
        )
    if _norm(claim.text) != _norm(cell.text):
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from cell")
    return VerifiedClaim(
        claim.claim_id,
        ClaimKind.CELL,
        claim.text,
        None,
        None,
        (
            ClaimCitation(
                block.member_id,
                block.kind,
                block.page_index,
                claim.field_path,
                (cell.cell_id, *cell.source_span_ids),
                cell.bbox,
                cell.text,
            ),
        ),
    )


def _chart_citation(
    block: ContextBlock, context: ChartContext, path: str, evidence: Evidence, quote: str
) -> ClaimCitation:
    field: FieldCitation = citation(context, path, evidence)
    return ClaimCitation(
        block.member_id,
        block.kind,
        block.page_index,
        path,
        evidence.element_ids,
        field.occurrences[0].anchor.bbox if field.occurrences else None,
        quote,
        field,
    )


def _value_claim(
    claim: ModelClaim,
    block: ContextBlock,
    context: ChartContext,
    point: ChartPoint,
    period: tuple[str, Evidence, str] | None,
) -> VerifiedClaim | RejectedClaim:
    value = point.value.value
    if point.value.kind is not ValueKind.EXPLICIT or value is None:
        return _reject(
            claim, AbstainReason.UNSUPPORTED_VALUE_KIND, f"value kind is {point.value.kind.value}"
        )
    if not _precision_supported(value):
        return _reject(claim, AbstainReason.UNSUPPORTED_PRECISION, "value precision unsupported")
    display = source_display(context, point)
    claimed = _decimal(claim.text) if _NUMBER_RE.fullmatch(claim.text.strip()) else None
    if _norm(claim.text) != _norm(display) and claimed != value:
        return _reject(
            claim,
            AbstainReason.CLAIM_NOT_IN_EVIDENCE,
            f"claimed {claim.text!r} differs from the source display {display!r}",
        )
    prefix = f"points.{point.point_id}"
    fields: list[tuple[str, Evidence, str]] = [
        (f"{prefix}.value", point.value.evidence, display),
        (f"{prefix}.series", point.series.evidence, point.series.text),
        (f"{prefix}.category", point.category.evidence, point.category.text),
        (f"{prefix}.unit", point.unit.evidence, point.unit.text),
    ]
    if period is not None:
        fields.append(period)
    return VerifiedClaim(
        claim.claim_id,
        ClaimKind.CHART_VALUE,
        display,
        value,
        point.unit.text,
        tuple(
            _chart_citation(block, context, path, evidence, text) for path, evidence, text in fields
        ),
    )


def _verify_donut(
    claim: ModelClaim, block: ContextBlock, context: ChartContext, point_id: str
) -> VerifiedClaim | RejectedClaim:
    check_context(context)
    chart = context.chart
    if (
        context.qualification.semantic_scope != _DONUT_SCOPE
        or chart.verification is not Verification.VERIFIED
        or context.description.verification is not Verification.VERIFIED
    ):
        return _reject(claim, AbstainReason.UNQUALIFIED_MEMBER, "member is not source-qualified")
    if chart.grammar != "donut":
        return _reject(claim, AbstainReason.UNSUPPORTED_GRAMMAR, f"grammar is {chart.grammar}")
    point = next((item for item in chart.points if item.point_id == point_id), None)
    if point is None:
        return _reject(claim, AbstainReason.UNKNOWN_POINT, f"point {point_id} is not in the chart")
    if point.value.kind is ValueKind.UNAVAILABLE:
        return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, "value is unavailable in source")
    check_fields(context)
    assert chart.period is not None  # check_fields proved it
    return _value_claim(
        claim, block, context, point, ("period", chart.period.evidence, chart.period.text)
    )


def _verify_displayed(
    claim: ModelClaim, block: ContextBlock, context: DisplayedLookupContext, point_id: str
) -> VerifiedClaim | RejectedClaim:
    if context.qualification.semantic_scope != DISPLAYED_BAR_SCOPE:
        return _reject(claim, AbstainReason.UNQUALIFIED_MEMBER, "member is not a displayed bar")
    if context.chart.grammar != "bar" or context.raw_chart.grammar != "bar":
        return _reject(
            claim, AbstainReason.UNSUPPORTED_GRAMMAR, f"grammar is {context.chart.grammar}"
        )
    evidence_context = chart_context(context)
    check_displayed_evidence(context)
    raw = next((item for item in context.raw_chart.points if item.point_id == point_id), None)
    if raw is None:
        return _reject(claim, AbstainReason.UNKNOWN_POINT, f"point {point_id} is not in the chart")
    if raw.value.kind is ValueKind.UNAVAILABLE:
        return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, "value is unavailable in source")
    point = next((item for item in context.chart.points if item.point_id == point_id), None)
    if point is None:
        return _reject(claim, AbstainReason.INSUFFICIENT_EVIDENCE, "point was not requalified")
    return _value_claim(claim, block, evidence_context, point, None)


def _verify_chart_value(
    claim: ModelClaim, block: ContextBlock, context: ChartContext | DisplayedLookupContext
) -> VerifiedClaim | RejectedClaim:
    match = _POINT_VALUE_RE.fullmatch(claim.field_path)
    if match is None:
        return _reject(
            claim, AbstainReason.MODEL_OUTPUT_INVALID, "chart claims cite points.<id>.value"
        )
    point_id = match.group("point")
    try:
        if isinstance(context, DisplayedLookupContext):
            return _verify_displayed(claim, block, context, point_id)
        return _verify_donut(claim, block, context, point_id)
    except FigureError as error:
        raise ChartQueryError(
            QueryFailure.INVALID_EVIDENCE, "Chart field/source evidence failed validation"
        ) from error


def verify_claims(
    model: ModelAnswer,
    blocks: Mapping[str, ContextBlock],
    *,
    chart_evidence: ChartEvidence,
) -> ClaimVerification:
    """Verify each claim independently; chart evidence is requalified lazily, once per member.

    ``ChartQueryError`` (corrupt or unavailable evidence) propagates; a chart refusal
    becomes a rejection with the same reason.
    """
    verified: list[VerifiedClaim] = []
    rejected: list[RejectedClaim] = []
    seen: set[str] = set()
    contexts: dict[str, ChartContext | DisplayedLookupContext] = {}
    for claim in model.claims:
        kind = ClaimKind(claim.kind)
        block = blocks.get(claim.member_id)
        if claim.claim_id in seen:
            rejected.append(
                _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "duplicate claim id")
            )
            continue
        seen.add(claim.claim_id)
        if block is None:
            rejected.append(_reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "unknown member"))
            continue
        if block.kind not in _BLOCK_KINDS[kind] or not claim.field_path.startswith(
            _PATH_PREFIX[kind]
        ):
            rejected.append(
                _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "claim kind or path mismatch")
            )
            continue
        if kind is ClaimKind.QUOTE:
            outcome = _verify_quote(claim, block)
        elif kind is ClaimKind.CELL:
            outcome = _verify_cell(claim, block)
        else:
            try:
                if claim.member_id not in contexts:
                    contexts[claim.member_id] = chart_evidence(claim.member_id)
                outcome = _verify_chart_value(claim, block, contexts[claim.member_id])
            except (ChartRefusal, DisplayedRefusal) as refusal:
                outcome = _reject(claim, from_refusal(refusal.reason), str(refusal))
        if isinstance(outcome, VerifiedClaim):
            verified.append(outcome)
        else:
            rejected.append(outcome)
    return ClaimVerification(tuple(verified), tuple(rejected))


def prose_grounded(
    answer: str, verified: Sequence[VerifiedClaim], *, question: str = ""
) -> tuple[bool, tuple[str, ...]]:
    """Every number or percentage in the prose must be grounded.

    A number is grounded when it equals a verified claim's text number or value, equals a
    number in the evidence text those claims cite (span quote, cell text, chart labels and
    source display), or appears verbatim in the user's question (a restated year or period
    is not a new figure). Anything else escapes and the whole answer abstains.
    """
    allowed: set[Decimal] = set()
    for claim in verified:
        allowed.update(value for _, value in _numbers(claim.text))
        if claim.value is not None:
            allowed.add(claim.value)
        for cited in claim.citations:
            allowed.update(value for _, value in _numbers(cited.quote))
    asked = {token for token, _ in _numbers(question)}
    escaped = sorted(
        {token for token, value in _numbers(answer) if value not in allowed and token not in asked}
    )
    return not escaped, tuple(escaped)


def decide(
    model: ModelAnswer,
    verification: ClaimVerification,
    *,
    blocks_present: bool,
    question: str = "",
) -> tuple[AnswerStatus, AbstainReason | None, str | None]:
    """Drop failed claims one by one; abstain on no verified claim or on ungrounded prose."""
    if not blocks_present:
        return AnswerStatus.ABSTAINED, AbstainReason.NO_RELEVANT_MEMBER, "no context block"
    if model.abstain:
        return AnswerStatus.ABSTAINED, AbstainReason.MODEL_DECLINED, model.abstain_reason
    if not verification.verified:
        if verification.rejected:
            first = verification.rejected[0]
            return AnswerStatus.ABSTAINED, first.reason, f"{first.claim_id}: {first.detail}"
        return AnswerStatus.ABSTAINED, AbstainReason.NO_VERIFIED_CLAIM, "the model cited no claim"
    grounded, escaped = prose_grounded(model.answer, verification.verified, question=question)
    if not grounded:
        dropped = ", ".join(claim.claim_id for claim in verification.rejected) or "none"
        return (
            AnswerStatus.ABSTAINED,
            AbstainReason.CLAIM_NOT_IN_EVIDENCE,
            f"numbers outside verified claims: {', '.join(escaped)}; rejected claims: {dropped}",
        )
    return AnswerStatus.ANSWERED, None, None
