"""Every claim is re-read from stored evidence; prose numbers must come from verified claims."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal, Never

import pytest

from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
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
from enterprise_pdf_rag.answers.verify import (
    ClaimVerification,
    decide,
    prose_grounded,
    verify_claims,
)
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    CellEvidence,
    ContextBlock,
    HeaderRef,
    SpanEvidence,
    build_context_block,
)
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.extraction.evidence.figures.chart_qa.displayed_models import (
    DisplayedLookupContext,
    DisplayedRefusal,
    DisplayedRefusalReason,
)
from ragspine.extraction.evidence.figures.chart_qa.models import (
    ChartContext,
    ChartQueryError,
    ChartRefusal,
    QueryFailure,
    RefusalReason,
)
from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.tables.table_models import CellContentState
from ragspine.extraction.evidence.page.models import ObjectKind
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import (
    DOCUMENT_LABEL,
    publish_generic_document,
    resolve_table_member,
)
from tests.enterprise_pdf_rag.answers.fake_document import (
    DIAGRAM_LABELS,
    diagram_member,
    formula_ir,
    formula_member,
)
from tests.enterprise_pdf_rag.answers.store_mounted_document import (
    StoreMountedDocument,
    bar_document,
    donut_document,
)

_SNAPSHOT = "5" * 64
_TEXT_MEMBER = "a" * 64
_TABLE_MEMBER = "b" * 64
_DIAGRAM_MEMBER = "diagram-1"
_FORMULA_MEMBER = "formula-1"
_RULED_MEMBER = "d" * 64
_ANCHOR = SourceAnchor("c" * 64, "c" * 64, 3, (0.0, 0.0, 100.0, 50.0))

type ChartEvidence = Callable[[str], ChartContext | DisplayedLookupContext]


def _text_block() -> ContextBlock:
    return ContextBlock(
        _SNAPSHOT,
        _TEXT_MEMBER,
        BlockKind.TEXT,
        3,
        "literal-source-transcription-v1",
        Verification.VERIFIED,
        "Revenue grew 12% in 2025. Costs fell.",
        spans=(
            SpanEvidence("sp-1", 3, (1.0, 10.0, 90.0, 19.0), "Revenue grew 12% in 2025."),
            SpanEvidence("sp-2", 3, (1.0, 20.0, 90.0, 29.0), "Costs   fell."),
        ),
    )


def _table_block() -> ContextBlock:
    return ContextBlock(
        _SNAPSHOT,
        _TABLE_MEMBER,
        BlockKind.TABLE,
        3,
        "literal-source-transcription-v1",
        Verification.VERIFIED,
        "Revenue",
        cells=(
            CellEvidence(
                "c-1",
                0,
                0,
                1,
                1,
                (1.0, 1.0, 40.0, 20.0),
                "1,234",
                CellContentState.PRESENT,
                ("t-1",),
            ),
            CellEvidence(
                "c-2", 0, 1, 1, 1, (41.0, 1.0, 90.0, 20.0), "", CellContentState.BLANK, ()
            ),
            CellEvidence(
                "c-3", 1, 0, 1, 2, (1.0, 21.0, 90.0, 40.0), None, CellContentState.UNAVAILABLE, ()
            ),
        ),
        row_count=2,
        col_count=2,
    )


def _verified_table_block() -> ContextBlock:
    """A table whose grid re-proved (ADR 0014): cells carry their row, column and header."""
    return ContextBlock(
        _SNAPSHOT,
        _RULED_MEMBER,
        BlockKind.TABLE,
        3,
        "literal-source-transcription-v1",
        Verification.VERIFIED,
        "Metric\nValue\nRevenue\n1,234",
        cells=(
            CellEvidence(
                "h-2",
                0,
                0,
                1,
                1,
                (1.0, 1.0, 40.0, 20.0),
                "Metric",
                CellContentState.PRESENT,
                ("t-h2",),
                Verification.VERIFIED,
            ),
            CellEvidence(
                "h-1",
                0,
                1,
                1,
                1,
                (41.0, 1.0, 90.0, 20.0),
                "Value",
                CellContentState.PRESENT,
                ("t-h1",),
                Verification.VERIFIED,
            ),
            CellEvidence(
                "c-2",
                1,
                0,
                1,
                1,
                (1.0, 21.0, 40.0, 40.0),
                "Revenue",
                CellContentState.PRESENT,
                ("t-2",),
                Verification.VERIFIED,
                (HeaderRef("h-2", "Metric", "row"),),
            ),
            CellEvidence(
                "c-1",
                1,
                1,
                1,
                1,
                (41.0, 21.0, 90.0, 40.0),
                "1,234",
                CellContentState.PRESENT,
                ("t-1",),
                Verification.VERIFIED,
                (HeaderRef("h-1", "Value", "row"),),
            ),
        ),
        row_count=2,
        col_count=2,
        grid_verification=Verification.VERIFIED,
    )


def _no_chart(member_id: str) -> Never:
    raise AssertionError("chart evidence must only be read for chart_value claims")


def _claim(
    member_id: str,
    kind: Literal["quote", "cell", "chart_value", "diagram_node", "diagram_edge", "formula"],
    field_path: str,
    text: str,
    *,
    claim_id: str = "c1",
    row: int | None = None,
    col: int | None = None,
    header: str | None = None,
) -> ModelClaim:
    return ModelClaim(
        claim_id=claim_id,
        member_id=member_id,
        kind=kind,
        field_path=field_path,
        text=text,
        row=row,
        col=col,
        header=header,
    )


def _answer(*claims: ModelClaim, answer: str = "") -> ModelAnswer:
    return ModelAnswer(abstain=False, abstain_reason=None, answer=answer, claims=claims)


def _diagram_block() -> ContextBlock:
    return build_context_block(diagram_member(_DIAGRAM_MEMBER))


def _formula_block() -> ContextBlock:
    return build_context_block(formula_member(_FORMULA_MEMBER))


def _verify(*claims: ModelClaim, chart_evidence: ChartEvidence = _no_chart) -> ClaimVerification:
    blocks = {
        _TEXT_MEMBER: _text_block(),
        _TABLE_MEMBER: _table_block(),
        _DIAGRAM_MEMBER: _diagram_block(),
        _FORMULA_MEMBER: _formula_block(),
        _RULED_MEMBER: _verified_table_block(),
    }
    return verify_claims(_answer(*claims), blocks, chart_evidence=chart_evidence)


def _only_rejected(verification: ClaimVerification) -> RejectedClaim:
    assert verification.verified == ()
    (rejected,) = verification.rejected
    return rejected


def test_every_chart_qa_refusal_maps_onto_an_abstain_reason() -> None:
    for reason in (*RefusalReason, *DisplayedRefusalReason):
        mapped = from_refusal(reason)
        assert isinstance(mapped, AbstainReason) and mapped.value == reason.value


def test_model_output_shape_is_strict() -> None:
    with pytest.raises(ValueError):
        ModelAnswer.model_validate_json('{"abstain": false, "answer": "x", "claims": []}')
    with pytest.raises(ValueError):
        ModelAnswer.model_validate_json(
            '{"abstain": false, "abstain_reason": null, "answer": "x", "claims": [], "extra": 1}'
        )
    with pytest.raises(ValueError, match="16"):
        _answer(
            *(
                _claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "R", claim_id=f"c{i}")
                for i in range(17)
            )
        )


def test_quote_claim_passes_only_as_a_verbatim_substring_of_its_span() -> None:
    verification = _verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "grew 12% in 2025"))
    (claim,) = verification.verified
    assert verification.rejected == ()
    assert (claim.claim_id, claim.kind, claim.text, claim.value, claim.unit) == (
        "c1",
        ClaimKind.QUOTE,
        "grew 12% in 2025",
        None,
        None,
    )
    (citation,) = claim.citations
    assert (citation.member_id, citation.kind, citation.page_index) == (
        _TEXT_MEMBER,
        BlockKind.TEXT,
        3,
    )
    assert citation.field_path == "fragments.sp-1"
    assert citation.evidence_ids == ("sp-1",)
    assert citation.bbox == (1.0, 10.0, 90.0, 19.0)
    assert citation.quote == "Revenue grew 12% in 2025."
    assert citation.chart_citation is None
    # Whitespace and case are normalised, nothing else.
    whitespace = _verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-2", "costs fell."))
    assert len(whitespace.verified) == 1


def test_quote_claim_is_rejected_when_absent_or_malformed() -> None:
    missing = _only_rejected(_verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "grew 13%")))
    assert (missing.reason, missing.member_id, missing.field_path) == (
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
        _TEXT_MEMBER,
        "fragments.sp-1",
    )
    empty = _only_rejected(_verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "   ")))
    assert empty.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    unknown_span = _only_rejected(_verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-9", "x")))
    assert unknown_span.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    unknown_member = _only_rejected(_verify(_claim("9" * 64, "quote", "fragments.sp-1", "x")))
    assert unknown_member.reason is AbstainReason.MODEL_OUTPUT_INVALID
    wrong_kind = _only_rejected(_verify(_claim(_TEXT_MEMBER, "cell", "cells.c-1", "1,234")))
    assert wrong_kind.reason is AbstainReason.MODEL_OUTPUT_INVALID
    wrong_path = _only_rejected(_verify(_claim(_TEXT_MEMBER, "quote", "cells.sp-1", "Revenue")))
    assert wrong_path.reason is AbstainReason.MODEL_OUTPUT_INVALID
    duplicate = _verify(
        _claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "Revenue"),
        _claim(_TEXT_MEMBER, "quote", "fragments.sp-2", "Costs"),
    )
    assert len(duplicate.verified) == 1
    assert duplicate.rejected[0].reason is AbstainReason.MODEL_OUTPUT_INVALID


def test_cell_claim_requires_the_exact_present_cell_text() -> None:
    verification = _verify(_claim(_TABLE_MEMBER, "cell", "cells.c-1", "1,234"))
    (claim,) = verification.verified
    assert claim.kind is ClaimKind.CELL and claim.value is None
    (citation,) = claim.citations
    assert citation.evidence_ids == ("c-1", "t-1")
    assert citation.bbox == (1.0, 1.0, 40.0, 20.0) and citation.quote == "1,234"
    wrong = _only_rejected(_verify(_claim(_TABLE_MEMBER, "cell", "cells.c-1", "1234")))
    assert wrong.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    blank = _only_rejected(_verify(_claim(_TABLE_MEMBER, "cell", "cells.c-2", "")))
    assert blank.reason is AbstainReason.VALUE_UNAVAILABLE
    unavailable = _only_rejected(_verify(_claim(_TABLE_MEMBER, "cell", "cells.c-3", "x")))
    assert unavailable.reason is AbstainReason.VALUE_UNAVAILABLE
    unknown = _only_rejected(_verify(_claim(_TABLE_MEMBER, "cell", "cells.c-9", "x")))
    assert unknown.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_diagram_node_claim_requires_the_exact_label_case_included() -> None:
    label = DIAGRAM_LABELS[0]
    verification = _verify(_claim(_DIAGRAM_MEMBER, "diagram_node", "nodes.n1.label", label))
    (claim,) = verification.verified
    assert verification.rejected == ()
    assert claim.kind is ClaimKind.DIAGRAM_NODE and claim.value is None
    (citation,) = claim.citations
    assert citation.kind is BlockKind.DIAGRAM and citation.page_index == 2
    assert citation.field_path == "nodes.n1.label"
    assert citation.evidence_ids == ("n1", "sp-plan")
    assert citation.bbox == (20.0, 70.0, 90.0, 100.0) and citation.quote == label
    # The label's own percentage is grounded by the claim it was copied from.
    assert prose_grounded(f"The first stage is {label}.", verification.verified) == (True, ())
    # Diagram labels are verbatim source text: folded whitespace passes, a case change does not.
    spaced = _verify(
        _claim(_DIAGRAM_MEMBER, "diagram_node", "nodes.n1.label", label.replace(": ", ":  "))
    )
    assert len(spaced.verified) == 1
    recased = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "diagram_node", "nodes.n1.label", label.casefold()))
    )
    assert recased.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    unknown = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "diagram_node", "nodes.n9.label", "Growth"))
    )
    assert unknown.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_diagram_edge_claim_requires_the_printed_pair_in_its_drawn_direction() -> None:
    printed = f"{DIAGRAM_LABELS[0]} -> {DIAGRAM_LABELS[1]}"
    verification = _verify(_claim(_DIAGRAM_MEMBER, "diagram_edge", "edges.0", printed))
    (claim,) = verification.verified
    assert claim.kind is ClaimKind.DIAGRAM_EDGE
    (citation,) = claim.citations
    assert citation.evidence_ids == ("edges.0", "n1", "n2")
    assert citation.bbox == (20.0, 70.0, 220.0, 100.0) and citation.quote == printed
    reversed_pair = f"{DIAGRAM_LABELS[1]} -> {DIAGRAM_LABELS[0]}"
    backwards = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "diagram_edge", "edges.0", reversed_pair))
    )
    assert backwards.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    missing = _only_rejected(_verify(_claim(_DIAGRAM_MEMBER, "diagram_edge", "edges.1", printed)))
    assert missing.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_diagram_claim_kind_and_path_must_agree() -> None:
    crossed = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "quote", "nodes.n1.label", DIAGRAM_LABELS[0]))
    )
    assert crossed.reason is AbstainReason.MODEL_OUTPUT_INVALID
    on_text = _only_rejected(
        _verify(_claim(_TEXT_MEMBER, "diagram_node", "nodes.n1.label", DIAGRAM_LABELS[0]))
    )
    assert on_text.reason is AbstainReason.MODEL_OUTPUT_INVALID
    malformed = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "diagram_node", "nodes.n1", DIAGRAM_LABELS[0]))
    )
    assert malformed.reason is AbstainReason.MODEL_OUTPUT_INVALID
    not_an_index = _only_rejected(
        _verify(_claim(_DIAGRAM_MEMBER, "diagram_edge", "edges.first", "x"))
    )
    assert not_an_index.reason is AbstainReason.MODEL_OUTPUT_INVALID


def _formula_line(path: str) -> str:
    ir = formula_ir()
    line = ir.linear if path == "formula.linear" else ir.readable
    assert line is not None
    return line


@pytest.mark.parametrize("path", ["formula.linear", "formula.readable"])
def test_formula_linear_claim_verifies_verbatim_only(path: str) -> None:
    line = _formula_line(path)
    verification = _verify(_claim(_FORMULA_MEMBER, "formula", path, line))
    (claim,) = verification.verified
    assert verification.rejected == ()
    assert claim.kind is ClaimKind.FORMULA and claim.value is None
    (citation,) = claim.citations
    assert citation.kind is BlockKind.FORMULA and citation.page_index == 4
    assert citation.field_path == path
    # A whole-line claim cites every span the proven tokens quote, and has no single box.
    assert citation.evidence_ids == ("sp-roe", "sp-num", "sp-den", "sp-scale")
    assert citation.bbox is None and citation.quote == line
    spaced = _verify(_claim(_FORMULA_MEMBER, "formula", path, line.replace(" ", "  ")))
    assert len(spaced.verified) == 1
    # Formula symbols are case sensitive: ``x`` is not ``X``.
    recased = _only_rejected(
        _verify(_claim(_FORMULA_MEMBER, "formula", path, line.replace("ROE", "roe")))
    )
    assert recased.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert recased.detail == "claim text differs from the formula line"
    blank = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "formula", path, "   ")))
    assert blank.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_formula_token_claim_cites_span_and_bbox() -> None:
    verification = _verify(_claim(_FORMULA_MEMBER, "formula", "tokens.4", "Equity"))
    (claim,) = verification.verified
    (citation,) = claim.citations
    assert citation.field_path == "tokens.4"
    assert citation.evidence_ids == ("sp-den",)
    assert citation.bbox == (72.0, 85.2, 111.6, 96.2) and citation.quote == "Equity"
    wrong = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "formula", "tokens.4", "Capital")))
    assert wrong.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert wrong.detail == "claim text differs from token"


def test_formula_unknown_token_index_is_rejected() -> None:
    unknown = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "formula", "tokens.9", "Equity")))
    assert unknown.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert unknown.detail == "cited token is not in the block"
    malformed = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "formula", "tokens.a", "Equity")))
    assert malformed.reason is AbstainReason.MODEL_OUTPUT_INVALID
    assert malformed.detail == (
        "formula claims cite formula.linear, formula.readable or tokens.<index>"
    )


def test_formula_path_prefix_mismatch_is_invalid_output() -> None:
    crossed = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "formula", "points.p-1.value", "100")))
    assert crossed.reason is AbstainReason.MODEL_OUTPUT_INVALID
    on_text = _only_rejected(_verify(_claim(_TEXT_MEMBER, "formula", "tokens.0", "ROE")))
    assert on_text.reason is AbstainReason.MODEL_OUTPUT_INVALID
    as_quote = _only_rejected(_verify(_claim(_FORMULA_MEMBER, "quote", "tokens.0", "ROE")))
    assert as_quote.reason is AbstainReason.MODEL_OUTPUT_INVALID


def test_prose_number_from_formula_token_is_grounded() -> None:
    verification = _verify(_claim(_FORMULA_MEMBER, "formula", "tokens.6", "100"))
    assert verification.rejected == ()
    assert prose_grounded("ROE 除以之后再乘以 100。", verification.verified) == (True, ())
    assert prose_grounded("ROE 乘以 250。", verification.verified) == (False, ("250",))


def test_cell_claim_with_row_col_header_verifies_against_the_grid() -> None:
    verification = _verify(
        _claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", row=1, col=1, header="Value")
    )
    (claim,) = verification.verified
    (citation,) = claim.citations
    assert (citation.row, citation.col, citation.header) == (1, 1, "Value")
    assert citation.header_cell_id == "h-1"
    assert citation.evidence_ids == ("c-1", "t-1", "h-1")
    # The literal-transcription criterion: whitespace collapses, case never does.
    spaced = _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", header=" Value\n"))
    assert spaced.verified[0].citations[0].header == "Value"
    plain = _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234"))
    (bare,) = plain.verified
    # A verified grid still reports the position it proved, even when nothing claimed it.
    assert (bare.citations[0].row, bare.citations[0].col) == (1, 1)
    assert bare.citations[0].header is None and bare.citations[0].evidence_ids == ("c-1", "t-1")


def test_cell_claim_relations_are_rejected_when_wrong_or_unverified() -> None:
    wrong_row = _only_rejected(
        _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", row=0, col=1))
    )
    assert wrong_row.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert wrong_row.detail == "claimed row/col differ from the cell"
    wrong_col = _only_rejected(
        _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", row=1, col=0))
    )
    assert wrong_col.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    # A header is cited verbatim: case is part of the column's name.
    miscased = _only_rejected(
        _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", header="value"))
    )
    assert miscased.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert miscased.detail.startswith("claimed header")
    other = _only_rejected(
        _verify(_claim(_RULED_MEMBER, "cell", "cells.c-1", "1,234", header="Metric"))
    )
    assert other.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    pending = _only_rejected(_verify(_claim(_TABLE_MEMBER, "cell", "cells.c-1", "1,234", row=0)))
    assert pending.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert pending.detail == "grid relations of this table are not verified"
    quote = _only_rejected(
        _verify(_claim(_TEXT_MEMBER, "quote", "fragments.sp-1", "Revenue", row=0))
    )
    assert quote.reason is AbstainReason.MODEL_OUTPUT_INVALID


def _chart_setup(
    document: StoreMountedDocument, member_id: str
) -> tuple[dict[str, ContextBlock], PinnedRetrievalHit]:
    hit = PinnedRetrievalHit(document.retrieval_snapshot_id, member_id, 1.0)
    block = build_context_block(document.resolve(hit))
    assert block.kind is BlockKind.CHART
    return {member_id: block}, hit


def test_bar_value_claims_are_read_back_from_the_displayed_source(tmp_path: Path) -> None:
    document, pin = bar_document(tmp_path)
    blocks, hit = _chart_setup(document, pin.member_id)
    reads = 0

    def evidence(member_id: str) -> DisplayedLookupContext:
        nonlocal reads
        reads += 1
        assert member_id == pin.member_id
        return document.displayed_context(hit)

    model = _answer(
        _claim(pin.member_id, "chart_value", "points.p-1H21.value", "15%", claim_id="ok"),
        _claim(pin.member_id, "chart_value", "points.p-1H23.value", "6", claim_id="bare"),
        _claim(pin.member_id, "chart_value", "points.p-1H23.value", "7%", claim_id="wrong"),
        _claim(pin.member_id, "chart_value", "points.p-1H22.value", "10%", claim_id="gap"),
        _claim(pin.member_id, "chart_value", "points.p-1H99.value", "1%", claim_id="none"),
        _claim(
            pin.member_id, "chart_value", "points.p-1H21.series", "Expense Ratio", claim_id="path"
        ),
    )
    verification = verify_claims(model, blocks, chart_evidence=evidence)
    assert reads == 1  # the displayed evidence is requalified once per member
    verified = {claim.claim_id: claim for claim in verification.verified}
    rejected = {claim.claim_id: claim for claim in verification.rejected}
    assert set(verified) == {"ok", "bare"} and set(rejected) == {"wrong", "gap", "none", "path"}
    ok = verified["ok"]
    assert (ok.kind, ok.text, ok.value, ok.unit) == (
        ClaimKind.CHART_VALUE,
        "15%",
        Decimal("15"),
        "%",
    )
    assert verified["bare"].text == "6%" and verified["bare"].value == Decimal("6")
    paths = [citation.field_path for citation in ok.citations]
    assert paths[0] == "points.p-1H21.value"
    assert set(paths) == {
        "points.p-1H21.value",
        "points.p-1H21.series",
        "points.p-1H21.category",
        "points.p-1H21.unit",
    }
    value = ok.citations[0]
    assert value.kind is BlockKind.CHART and value.member_id == pin.member_id
    assert value.quote == "15%" and value.evidence_ids and value.bbox is not None
    assert value.chart_citation is not None
    assert value.chart_citation.field_path == "points.p-1H21.value"
    assert value.chart_citation.occurrences and value.chart_citation.svg_digest
    assert rejected["wrong"].reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert rejected["gap"].reason is AbstainReason.VALUE_UNAVAILABLE
    assert rejected["none"].reason is AbstainReason.UNKNOWN_POINT
    assert rejected["path"].reason is AbstainReason.MODEL_OUTPUT_INVALID


def test_donut_value_claims_pass_check_fields_and_source_display(tmp_path: Path) -> None:
    document, pin = donut_document(tmp_path)
    blocks, hit = _chart_setup(document, pin.member_id)
    context = document.chart_context(hit)
    point = context.chart.points[0]
    assert point.value.value is not None
    display = f"{point.value.value}%"
    model = _answer(
        _claim(pin.member_id, "chart_value", f"points.{point.point_id}.value", display),
        _claim(
            pin.member_id, "chart_value", f"points.{point.point_id}.value", "0.5%", claim_id="c2"
        ),
    )
    verification = verify_claims(model, blocks, chart_evidence=lambda _: context)
    (claim,) = verification.verified
    assert claim.value == point.value.value and claim.text == display
    assert {c.field_path for c in claim.citations} == {
        f"points.{point.point_id}.value",
        f"points.{point.point_id}.series",
        f"points.{point.point_id}.category",
        f"points.{point.point_id}.unit",
        "period",
    }
    (rejected,) = verification.rejected
    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE


def test_chart_refusals_become_rejections_and_evidence_errors_propagate(tmp_path: Path) -> None:
    document, pin = bar_document(tmp_path)
    blocks, _ = _chart_setup(document, pin.member_id)
    claim = _claim(pin.member_id, "chart_value", "points.p-1H21.value", "15%")

    def refused(member_id: str) -> Never:
        raise DisplayedRefusal(DisplayedRefusalReason.UNQUALIFIED_MEMBER)

    def refused_donut(member_id: str) -> Never:
        raise ChartRefusal(RefusalReason.UNSUPPORTED_GRAMMAR)

    def corrupt(member_id: str) -> Never:
        raise ChartQueryError(QueryFailure.INVALID_EVIDENCE, "tampered")

    assert (
        _only_rejected(verify_claims(_answer(claim), blocks, chart_evidence=refused)).reason
        is AbstainReason.UNQUALIFIED_MEMBER
    )
    assert (
        _only_rejected(verify_claims(_answer(claim), blocks, chart_evidence=refused_donut)).reason
        is AbstainReason.UNSUPPORTED_GRAMMAR
    )
    with pytest.raises(ChartQueryError):
        verify_claims(_answer(claim), blocks, chart_evidence=corrupt)
    # Quote claims never touch chart evidence.
    assert verify_claims(_answer(), blocks, chart_evidence=corrupt) == ClaimVerification((), ())


def _verified(text: str, value: Decimal | None = None) -> VerifiedClaim:
    return VerifiedClaim("v", ClaimKind.QUOTE, text, value, None, ())


def test_prose_numbers_must_equal_a_verified_claim_value() -> None:
    claims = (_verified("15%", Decimal("15")), _verified("Revenue grew 12% in 2025."))
    assert prose_grounded("In 1H21 the ratio was 15 % and revenue grew 12% in 2025.", claims) == (
        True,
        (),
    )
    assert prose_grounded("The ratio was 15.0%.", claims) == (True, ())
    assert prose_grounded("No figures here.", claims) == (True, ())
    ok, tokens = prose_grounded("It fell 9 points to 6% (from 15%).", claims)
    assert not ok and tokens == ("6%", "9")
    assert prose_grounded("about 1,500", (_verified("1,500"),)) == (True, ())
    assert prose_grounded("about 1500", (_verified("1,500"),)) == (True, ())
    assert prose_grounded("FY2024 revenue", ()) == (True, ())  # alphanumeric labels are not numbers
    assert prose_grounded("12%", ())[0] is False


def _cited(text: str, *quotes: str, value: Decimal | None = None) -> VerifiedClaim:
    citations = tuple(
        ClaimCitation(_TEXT_MEMBER, BlockKind.TEXT, 3, "fragments.sp-1", ("sp-1",), None, quote)
        for quote in quotes
    )
    return VerifiedClaim("v", ClaimKind.QUOTE, text, value, None, citations)


def test_prose_numbers_from_the_question_or_the_cited_evidence_are_grounded() -> None:
    claims = (_verified("17.5%", Decimal("17.5")),)
    question = "What was the operating ROE in 1H 2026?"
    # (b) a number repeated verbatim from the question — the year — is not a new figure.
    assert prose_grounded("The operating ROE in 1H 2026 was 17.5%.", claims, question=question) == (
        True,
        (),
    )
    assert prose_grounded("In 1H 2026 it was 17.5%.", claims) == (False, ("2026",))
    # A number in neither the question nor the evidence still escapes.
    ok, tokens = prose_grounded(
        "In 1H 2026 it was 17.5%, up from 16.1%.", claims, question=question
    )
    assert not ok and tokens == ("16.1%",)
    # (c) numbers in the cited evidence text ground the prose even outside the claim text.
    quoted = (_cited("grew 12%", "Revenue grew 12% in 2025."),)
    assert prose_grounded("Revenue grew 12% in 2025.", quoted) == (True, ())
    assert prose_grounded("Revenue grew 12% in 2024.", quoted) == (False, ("2024",))
    # Chart claims cite their period / category labels and the source display.
    chart = (_cited("17.5%", "17.5%", "1H 2026", "%", value=Decimal("17.5")),)
    assert prose_grounded("It reached 17.5% in 1H 2026.", chart) == (True, ())


def test_prose_enumerators_at_a_line_or_sentence_start_are_not_figures() -> None:
    stages = tuple(_verified(label) for label in ("Foundation", "Growth", "Scale"))
    listed = "The three stages are:\n1. Foundation\n2. Growth\n3. Scale"
    assert prose_grounded(listed, stages) == (True, ())
    assert prose_grounded("  1) Foundation\n  2) Growth\n  3) Scale", stages) == (True, ())
    assert prose_grounded("(1) Foundation; (2) Growth; (3) Scale.", stages) == (True, ())
    assert prose_grounded("① Foundation ② Growth ③ Scale", stages) == (True, ())
    assert prose_grounded("第 1 阶段是 Foundation。第 2 阶段是 Growth。", stages) == (True, ())
    assert prose_grounded("Step 1: Foundation. Step 2: Growth. Step 3: Scale.", stages) == (
        True,
        (),
    )
    # Only a line or sentence start is an enumerator; the same token inside a sentence is
    # a figure, and so is every amount, percentage or year in the body of an item.
    assert prose_grounded("Foundation is stage 1. of 3", stages) == (False, ("1", "3"))
    assert prose_grounded("1. Foundation reached 45% of agents", stages) == (False, ("45%",))
    assert prose_grounded("1. Foundation\n2. Growth (2025)", stages) == (False, ("2025",))
    assert prose_grounded("1. Foundation costs 1,500", stages) == (False, ("1,500",))
    # A decimal at a sentence end is never mistaken for an enumerator.
    ratio = (_verified("17.5%", Decimal("17.5")),)
    assert prose_grounded("The ratio was 17.5. It held.", ratio) == (True, ())
    assert prose_grounded("The ratio was 17.5.\n2. It held.", ratio) == (True, ())


def test_decide_answers_a_numbered_list_but_not_a_number_outside_the_evidence() -> None:
    stages = tuple(_verified(label) for label in ("Foundation", "Growth", "Scale"))
    verification = ClaimVerification(stages, ())
    listed = _answer(answer="1. Foundation\n2. Growth\n3. Scale")
    assert decide(listed, verification, blocks_present=True) == (AnswerStatus.ANSWERED, None, None)
    invented = _answer(answer="1. Foundation (45%)\n2. Growth\n3. Scale")
    status, reason, detail = decide(invented, verification, blocks_present=True)
    assert status is AnswerStatus.ABSTAINED and reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert detail is not None and "45%" in detail


def test_decide_admits_question_and_evidence_numbers_but_not_new_ones() -> None:
    verified = (_cited("17.5%", "record Operating ROE of 17.5%", value=Decimal("17.5")),)
    question = "What was the operating ROE in 1H 2026?"
    answered = _answer(answer="The operating ROE in 1H 2026 was 17.5%.")
    assert decide(
        answered, ClaimVerification(verified, ()), blocks_present=True, question=question
    ) == (AnswerStatus.ANSWERED, None, None)
    assert decide(answered, ClaimVerification(verified, ()), blocks_present=True)[:2] == (
        AnswerStatus.ABSTAINED,
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
    )
    invented = _answer(answer="The operating ROE in 1H 2026 was 17.5%, up from 16.1%.")
    status, reason, detail = decide(
        invented, ClaimVerification(verified, ()), blocks_present=True, question=question
    )
    assert (status, reason) == (AnswerStatus.ABSTAINED, AbstainReason.CLAIM_NOT_IN_EVIDENCE)
    assert detail is not None and "16.1%" in detail and "2026" not in detail
    # Zero verified claims are unchanged: the question never grounds an answer by itself.
    none = decide(answered, ClaimVerification((), ()), blocks_present=True, question=question)
    assert none[:2] == (AnswerStatus.ABSTAINED, AbstainReason.NO_VERIFIED_CLAIM)


def test_decide_applies_the_rejection_then_prose_gate_policy() -> None:
    verified = (_verified("15%", Decimal("15")),)
    rejected = (
        RejectedClaim(
            "c2", "m", "points.p-1H22.value", "10%", AbstainReason.VALUE_UNAVAILABLE, "gap"
        ),
        RejectedClaim("c3", "m", "fragments.x", "y", AbstainReason.CLAIM_NOT_IN_EVIDENCE, "no"),
    )
    answer = _answer(answer="The ratio was 15%.")
    assert decide(answer, ClaimVerification(verified, rejected), blocks_present=True) == (
        AnswerStatus.ANSWERED,
        None,
        None,
    )
    assert decide(answer, ClaimVerification(verified, ()), blocks_present=False)[:2] == (
        AnswerStatus.ABSTAINED,
        AbstainReason.NO_RELEVANT_MEMBER,
    )
    declined = ModelAnswer(abstain=True, abstain_reason="ambiguous", answer="", claims=())
    assert decide(declined, ClaimVerification(verified, ()), blocks_present=True) == (
        AnswerStatus.ABSTAINED,
        AbstainReason.MODEL_DECLINED,
        "ambiguous",
    )
    first = decide(answer, ClaimVerification((), rejected), blocks_present=True)
    assert first[:2] == (AnswerStatus.ABSTAINED, AbstainReason.VALUE_UNAVAILABLE)
    none = decide(answer, ClaimVerification((), ()), blocks_present=True)
    assert none[:2] == (AnswerStatus.ABSTAINED, AbstainReason.NO_VERIFIED_CLAIM)
    gated = decide(
        _answer(answer="It fell 9 points to 6%."),
        ClaimVerification(verified, rejected),
        blocks_present=True,
    )
    assert gated[:2] == (AnswerStatus.ABSTAINED, AbstainReason.CLAIM_NOT_IN_EVIDENCE)
    assert gated[2] is not None and "6%" in gated[2] and "9" in gated[2] and "c2" in gated[2]


def test_chart_kind_mismatch_with_block_is_invalid_output(tmp_path: Path) -> None:
    document, pin = bar_document(tmp_path)
    blocks, _ = _chart_setup(document, pin.member_id)
    (footer,) = document.member_ids_by_kind(ObjectKind.TEXT)
    quote_on_chart = verify_claims(
        _answer(_claim(pin.member_id, "quote", "fragments.x", "Expense")),
        blocks,
        chart_evidence=_no_chart,
    )
    assert _only_rejected(quote_on_chart).reason is AbstainReason.MODEL_OUTPUT_INVALID
    chart_on_missing = verify_claims(
        _answer(_claim(footer, "chart_value", "points.p-1H21.value", "15%")),
        blocks,
        chart_evidence=_no_chart,
    )
    assert _only_rejected(chart_on_missing).reason is AbstainReason.MODEL_OUTPUT_INVALID


def test_published_native_table_cells_verify_verbatim_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = publish_generic_document(
        tmp_path,
        monkeypatch,
        filename="meridian-semiannual.pdf",
        label=DOCUMENT_LABEL,
        page_count=3,
        embedder=OfflineDescriptionEmbedder(),
        table_page=True,
    )
    block = build_context_block(resolve_table_member(published))
    assert block.kind is BlockKind.TABLE
    member = block.member_id
    value = next(cell for cell in block.cells if cell.text == "1,234")
    blank = next(cell for cell in block.cells if cell.content_state is CellContentState.BLANK)
    blocks = {member: block}

    def run(*claims: ModelClaim) -> ClaimVerification:
        return verify_claims(_answer(*claims), blocks, chart_evidence=_no_chart)

    exact = _claim(member, "cell", f"cells.{value.cell_id}", "1,234")
    verification = run(exact)
    (claim,) = verification.verified
    assert claim.kind is ClaimKind.CELL and claim.text == "1,234" and claim.value is None
    (citation,) = claim.citations
    assert citation.member_id == member and citation.kind is BlockKind.TABLE
    assert citation.page_index == 2 and citation.field_path == f"cells.{value.cell_id}"
    assert citation.evidence_ids == (value.cell_id, *value.source_span_ids)
    assert len(value.source_span_ids) == 1 and citation.bbox == value.bbox
    assert citation.quote == "1,234"

    # Values are never derived or reformatted: any numeric drift is dropped.
    for drifted in ("1234", "1,235", "1,234.0", "1 234"):
        rejected = _only_rejected(run(_claim(member, "cell", f"cells.{value.cell_id}", drifted)))
        assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE, drifted
    blank_claim = _only_rejected(run(_claim(member, "cell", f"cells.{blank.cell_id}", "0")))
    assert blank_claim.reason is AbstainReason.VALUE_UNAVAILABLE
    missing = _only_rejected(run(_claim(member, "cell", "cells.not-a-cell", "1,234")))
    assert missing.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE

    assert decide(
        _answer(exact, answer="Revenue is 1,234."), verification, blocks_present=True
    ) == (
        AnswerStatus.ANSWERED,
        None,
        None,
    )
    status, reason, _detail = decide(
        _answer(exact, answer="Revenue is 1,235."), verification, blocks_present=True
    )
    assert (status, reason) == (AnswerStatus.ABSTAINED, AbstainReason.CLAIM_NOT_IN_EVIDENCE)


def test_prose_numbers_from_the_page_context_are_grounded_and_nothing_else_is() -> None:
    claims = (_verified("17.5%", Decimal("17.5")),)
    page = (
        "[page_context page_index=3] title=Group performance\n"
        "(page context: understanding only; it carries no citable path)\n"
        "- (text) Costs fell 4.2% in the period."
    )
    # The default is unchanged: without the page context the same prose escapes.
    assert prose_grounded("ROE was 17.5% while costs fell 4.2%.", claims) == (False, ("4.2%",))
    assert prose_grounded("ROE was 17.5%.", claims) == (True, ())
    assert prose_grounded(
        "ROE was 17.5% while costs fell 4.2%.", claims, context_texts=(page,)
    ) == (
        True,
        (),
    )
    # The boundary: a number in neither the claims nor the page context still escapes.
    ok, tokens = prose_grounded("ROE was 17.5%, up from 16.1%.", claims, context_texts=(page,))
    assert not ok and tokens == ("16.1%",)
    # A page context alone never grounds an answer that cites nothing.
    assert prose_grounded("Costs fell 4.2%.", (), context_texts=(page,)) == (True, ())


def test_decide_passes_the_page_context_through_to_the_prose_gate() -> None:
    verified = (_verified("17.5%", Decimal("17.5")),)
    page = "[page_context page_index=3]\n- (text) Costs fell 4.2% in the period."
    answered = _answer(answer="ROE was 17.5% while costs fell 4.2%.")
    assert decide(answered, ClaimVerification(verified, ()), blocks_present=True)[:2] == (
        AnswerStatus.ABSTAINED,
        AbstainReason.CLAIM_NOT_IN_EVIDENCE,
    )
    assert decide(
        answered, ClaimVerification(verified, ()), blocks_present=True, context_texts=(page,)
    ) == (AnswerStatus.ANSWERED, None, None)


def test_a_question_number_is_grounded_whatever_punctuation_follows_it() -> None:
    """The comma after ``In 2024,`` is sentence punctuation, not a thousands separator.

    ``_NUMBER_RE`` reads thousands separators, so it captures that comma into the token.
    Matching the question's numbers by their written form alone therefore missed a bare
    year the model had merely restated (live `p02-year-filter-2024-en`, 2026-09-21).
    """
    claims = (_verified("+11%", Decimal("11")),)
    asked = "What was the VONB growth in 2024?"
    assert prose_grounded("In 2024, VONB growth was +11%.", claims, question=asked) == (True, ())
    assert prose_grounded("VONB growth in 2024 was +11%.", claims, question=asked) == (True, ())
    interim = "What was the VONB growth in 2026?"
    assert prose_grounded("In 2026, VONB grew +11%.", claims, question=interim) == (True, ())
    # A thousands separator inside the figure still reads as one, on either side.
    assert prose_grounded("It reached 1,024 agents.", (), question="How many of 1024?") == (
        True,
        (),
    )
    # The percent sign stays part of the form: a bare number in the question does not
    # ground a percentage in the prose.
    ok, tokens = prose_grounded("Growth was 11%.", (), question="Was the growth 11 or more?")
    assert not ok and tokens == ("11%",)
    # A year the question never names still escapes.
    ok, tokens = prose_grounded("In 2023, VONB grew +11%.", claims, question=asked)
    assert not ok and tokens == ("2023,",)
