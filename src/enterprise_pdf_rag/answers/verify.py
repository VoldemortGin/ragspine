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
    check_point_fields,
    citation,
    source_display,
    verbatim_display,
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
from enterprise_pdf_rag.processing.context_builder import BlockKind, ContextBlock, HeaderRef
from enterprise_pdf_rag.processing.table_models import CellContentState

type ChartEvidence = Callable[[str], ChartContext | DisplayedLookupContext]

_DONUT_SCOPE = "explicit-distribution-shares"
# ``adapters.figure_label_qualification.FIGURE_POINT_SCOPE``. ``answers`` is a pure package and
# may not import an adapter, so the string is mirrored here exactly as ``_DONUT_SCOPE`` is;
# ``tests/enterprise_pdf_rag/answers/test_verify_verbatim_points.py`` pins the two together.
_POINT_SCOPE = "source-labels-and-verbatim-points-v1"
# One kind may own several prefixes; ``str.startswith`` accepts the tuple as is.
_PATH_PREFIX = {
    ClaimKind.QUOTE: ("fragments.",),
    ClaimKind.CELL: ("cells.",),
    ClaimKind.CHART_VALUE: ("points.",),
    ClaimKind.DIAGRAM_NODE: ("nodes.",),
    ClaimKind.DIAGRAM_EDGE: ("edges.",),
    ClaimKind.FORMULA: ("formula.", "tokens."),
}
_BLOCK_KINDS = {
    ClaimKind.QUOTE: {BlockKind.TEXT, BlockKind.LIST, BlockKind.GROUP},
    ClaimKind.CELL: {BlockKind.TABLE},
    ClaimKind.CHART_VALUE: {BlockKind.CHART},
    ClaimKind.DIAGRAM_NODE: {BlockKind.DIAGRAM},
    ClaimKind.DIAGRAM_EDGE: {BlockKind.DIAGRAM},
    ClaimKind.FORMULA: {BlockKind.FORMULA},
}
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*%?(?![\w%])")
# An enumeration marker at the start of a line or of a sentence — ``1.`` / ``2)`` / ``3、`` /
# ``(4)`` / ``第 5`` / ``Step 6`` — numbers the items of a list; it is not a figure. Inside a
# sentence the same token stays a number, and a decimal ending a sentence (``17.5.``) is
# untouched because a sentence start needs whitespace after ASCII punctuation (fullwidth CJK
# punctuation is never a decimal point, so no whitespace is required after it).
_ENUMERATOR_RE = re.compile(
    r"(?:^\s*|(?<=[.!?;:])\s+|(?<=[。！？；：])\s*)"  # noqa: RUF001 — fullwidth CJK sentence punctuation is matched on purpose, never its ASCII lookalike
    r"(?:\(\d{1,2}\)|\d{1,2}[.)、]|(?:Step|第)\s\d{1,2})"
    r"(?=\s|[:：]|$)",  # noqa: RUF001 — the fullwidth colon after ``第 1`` / ``Step 1`` is the CJK form
    re.MULTILINE,
)
_POINT_VALUE_RE = re.compile(r"points\.(?P<point>[^.]+)\.value")
_NODE_LABEL_RE = re.compile(r"nodes\.(?P<node>[A-Za-z0-9_-]+)\.label")
_EDGE_RE = re.compile(r"edges\.(?P<index>\d+)")
_TOKEN_PATH_RE = re.compile(r"tokens\.(?P<index>\d+)")


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    verified: tuple[VerifiedClaim, ...]
    rejected: tuple[RejectedClaim, ...]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _exact(text: str) -> str:
    """The literal-transcription criterion: whitespace collapses, case is kept."""
    return " ".join(text.split())


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
    header: HeaderRef | None = None
    if any(value is not None for value in (claim.row, claim.col, claim.header)):
        if (
            block.grid_verification is not Verification.VERIFIED
            or cell.verification is not Verification.VERIFIED
        ):
            return _reject(
                claim,
                AbstainReason.CLAIM_NOT_IN_EVIDENCE,
                "grid relations of this table are not verified",
            )
        if (claim.row is not None and claim.row != cell.row) or (
            claim.col is not None and claim.col != cell.col
        ):
            return _reject(
                claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claimed row/col differ from the cell"
            )
        if claim.header is not None:
            # Literal, never ``_norm``: a header names a column, and its case is part of it.
            wanted = _exact(claim.header)
            header = next((ref for ref in cell.headers if _exact(ref.text) == wanted), None)
            if header is None:
                return _reject(
                    claim,
                    AbstainReason.CLAIM_NOT_IN_EVIDENCE,
                    "claimed header is not a proved header of the cell",
                )
    grid_verified = block.grid_verification is Verification.VERIFIED
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
                (cell.cell_id, *cell.source_span_ids, *((header.cell_id,) if header else ())),
                cell.bbox,
                cell.text,
                row=cell.row if grid_verified else None,
                col=cell.col if grid_verified else None,
                header=None if header is None else header.text,
                header_cell_id=None if header is None else header.cell_id,
            ),
        ),
    )


def _verify_formula(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    """A formula line or one proven token, compared verbatim: symbols are case sensitive."""
    if claim.field_path in ("formula.linear", "formula.readable"):
        expected = (
            block.formula_linear if claim.field_path == "formula.linear" else block.formula_readable
        )
        if expected is None:
            return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, "formula line is unavailable")
        if not claim.text.strip() or _exact(claim.text) != _exact(expected):
            return _reject(
                claim,
                AbstainReason.CLAIM_NOT_IN_EVIDENCE,
                "claim text differs from the formula line",
            )
        citation = ClaimCitation(
            block.member_id,
            block.kind,
            block.page_index,
            claim.field_path,
            tuple(dict.fromkeys(token.source_span_id for token in block.formula_tokens)),
            None,
            expected,
        )
    else:
        match = _TOKEN_PATH_RE.fullmatch(claim.field_path)
        if match is None:
            return _reject(
                claim,
                AbstainReason.MODEL_OUTPUT_INVALID,
                "formula claims cite formula.linear, formula.readable or tokens.<index>",
            )
        index = int(match.group("index"))
        token = next((item for item in block.formula_tokens if item.index == index), None)
        if token is None:
            return _reject(
                claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited token is not in the block"
            )
        if _exact(claim.text) != _exact(token.text):
            return _reject(
                claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from token"
            )
        citation = ClaimCitation(
            block.member_id,
            block.kind,
            block.page_index,
            claim.field_path,
            (token.source_span_id,),
            token.bbox,
            token.text,
        )
    return VerifiedClaim(claim.claim_id, ClaimKind.FORMULA, claim.text, None, None, (citation,))


def _verify_diagram_node(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    match = _NODE_LABEL_RE.fullmatch(claim.field_path)
    if match is None:
        return _reject(
            claim, AbstainReason.MODEL_OUTPUT_INVALID, "diagram node claims cite nodes.<id>.label"
        )
    node = next((item for item in block.nodes if item.node_id == match.group("node")), None)
    if node is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited node is not in the block")
    if _exact(claim.text) != _exact(node.label):
        return _reject(
            claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from node label"
        )
    return VerifiedClaim(
        claim.claim_id,
        ClaimKind.DIAGRAM_NODE,
        claim.text,
        None,
        None,
        (
            ClaimCitation(
                block.member_id,
                block.kind,
                block.page_index,
                claim.field_path,
                (node.node_id, *node.source_span_ids),
                node.bbox,
                node.label,
            ),
        ),
    )


def _verify_diagram_edge(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    match = _EDGE_RE.fullmatch(claim.field_path)
    if match is None:
        return _reject(
            claim, AbstainReason.MODEL_OUTPUT_INVALID, "diagram edge claims cite edges.<index>"
        )
    edge = next(
        (item for item in block.edges if item.edge_index == int(match.group("index"))), None
    )
    if edge is None:
        return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited edge is not in the block")
    if _exact(claim.text) != _exact(edge.value):
        return _reject(
            claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from the printed edge"
        )
    return VerifiedClaim(
        claim.claim_id,
        ClaimKind.DIAGRAM_EDGE,
        claim.text,
        None,
        None,
        (
            ClaimCitation(
                block.member_id,
                block.kind,
                block.page_index,
                claim.field_path,
                (claim.field_path, edge.source_node_id, edge.target_node_id),
                edge.bbox,
                edge.value,
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


def _magnitude_supported(value: Decimal) -> bool:
    """Any printed magnitude. The 0-100 clamp in ``_precision_supported`` is the donut share's."""
    exponent = value.as_tuple().exponent
    return len(value.as_tuple().digits) <= 34 and isinstance(exponent, int) and -28 <= exponent <= 2


def _value_claim(
    claim: ModelClaim,
    block: ContextBlock,
    context: ChartContext,
    point: ChartPoint,
    period: tuple[str, Evidence, str] | None,
    *,
    printed: Callable[[ChartContext, ChartPoint], str] = source_display,
    supported: Callable[[Decimal], bool] = _precision_supported,
) -> VerifiedClaim | RejectedClaim:
    value = point.value.value
    if point.value.kind is not ValueKind.EXPLICIT or value is None:
        return _reject(
            claim, AbstainReason.UNSUPPORTED_VALUE_KIND, f"value kind is {point.value.kind.value}"
        )
    if not supported(value):
        return _reject(claim, AbstainReason.UNSUPPORTED_PRECISION, "value precision unsupported")
    display = printed(context, point)
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


def _verify_verbatim_points(
    claim: ModelClaim, block: ContextBlock, context: ChartContext, point_id: str
) -> VerifiedClaim | RejectedClaim:
    """Re-read one point of a verbatim-points member (ADR 0016), not the ADR 0008 closure.

    The projected chart stays ``PENDING`` on purpose — the category-to-value association is
    the model's, not a proof — so the verification lives on the *point*: its four fields must
    each be VERIFIED and proved by the receipt, and its number must be printed in the figure.
    Grammar is not gated: bar, donut and waterfall all project the same way here.
    """
    check_context(context)
    chart = context.chart
    if (
        context.qualification.semantic_scope != _POINT_SCOPE
        or context.description.verification is not Verification.VERIFIED
    ):
        return _reject(claim, AbstainReason.UNQUALIFIED_MEMBER, "member is not source-qualified")
    point = next((item for item in chart.points if item.point_id == point_id), None)
    if point is None:
        return _reject(claim, AbstainReason.UNKNOWN_POINT, f"point {point_id} is not in the chart")
    if point.value.kind is ValueKind.UNAVAILABLE:
        return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, "value is unavailable in source")
    check_point_fields(context, point)
    period = None if chart.period is None else ("period", chart.period.evidence, chart.period.text)
    return _value_claim(
        claim,
        block,
        context,
        point,
        period,
        printed=verbatim_display,
        supported=_magnitude_supported,
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
        if context.qualification.semantic_scope == _POINT_SCOPE:
            return _verify_verbatim_points(claim, block, context, point_id)
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
        if kind is not ClaimKind.CELL and any(
            value is not None for value in (claim.row, claim.col, claim.header)
        ):
            rejected.append(
                _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "grid relations need a cell")
            )
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
        elif kind is ClaimKind.FORMULA:
            outcome = _verify_formula(claim, block)
        elif kind is ClaimKind.DIAGRAM_NODE:
            outcome = _verify_diagram_node(claim, block)
        elif kind is ClaimKind.DIAGRAM_EDGE:
            outcome = _verify_diagram_edge(claim, block)
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
    is not a new figure). An enumeration marker opening a line or a sentence (``1.``,
    ``(2)``, ``第 3``, ``Step 4``) numbers a list item and is not a figure. Anything else
    escapes and the whole answer abstains.
    """
    allowed: set[Decimal] = set()
    for claim in verified:
        allowed.update(value for _, value in _numbers(claim.text))
        if claim.value is not None:
            allowed.add(claim.value)
        for cited in claim.citations:
            allowed.update(value for _, value in _numbers(cited.quote))
    asked = {token for token, _ in _numbers(question)}
    prose = _ENUMERATOR_RE.sub(" ", answer)
    escaped = sorted(
        {token for token, value in _numbers(prose) if value not in allowed and token not in asked}
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
