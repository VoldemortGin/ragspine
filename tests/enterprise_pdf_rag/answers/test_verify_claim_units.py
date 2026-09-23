"""A ``chart_value`` claim may print its unit; the unit is split off and proved, never dropped.

``prompt.SYSTEM_RULES`` asks for "the displayed value with its unit", which was written for a
percentage figure that prints its own ``%``. A ``$m`` figure prints ``294`` under a
``VONB ($m)`` caption, so the evidence block says ``unit=$m value=294`` and the model writes
``294$m`` / ``232 $m`` — and the verifier compared that against the source display ``294``
verbatim and refused the answer (live `k01` / `k02`, 2026-09-21). The rule here: the unit is
split off the claim deterministically and must be the point's own unit, and what is left must
still be the display verbatim.
"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.figure_label_qualification import (
    FIGURE_POINT_SCOPE,
    qualify_source_labels,
)
from enterprise_pdf_rag.answers.models import AbstainReason, ClaimKind
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from enterprise_pdf_rag.answers.verify import ClaimVerification, _split_unit, verify_claims
from enterprise_pdf_rag.processing.context_builder import (
    BlockKind,
    ContextBlock,
    build_context_block,
)
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.extraction.evidence.figures.chart_qa.models import ChartContext, QueryPin
from ragspine.extraction.evidence.figures.models import (
    ChartAxis,
    ChartIR,
    SvgArtifact,
    TextField,
    Verification,
)
from tests.enterprise_pdf_rag.adapters.test_figure_label_qualification import (
    _chart,
    _description,
    _evidence,
    _field,
    _figure,
    _label,
    _point,
)
from tests.enterprise_pdf_rag.answers.store_mounted_document import bar_document

MEMBER = "b" * 64
PIN = QueryPin("a" * 64, "c" * 64, MEMBER)
POINT = "points.point-1h26.value"

_CAPTION = ("VONB ($m)", (100.0, 10.0, 180.0, 22.0))
_CATEGORY = ("1H26", (100.0, 80.0, 130.0, 92.0))


def _money(printed: str, *, unit_beside_the_number: bool = False) -> ChartContext:
    """A ``$m`` bar printing ``printed`` once, with its unit beside it or in the caption."""
    number = (printed, (100.0, 40.0, 124.0, 52.0))
    beside = ("$m", (125.0, 40.0, 140.0, 52.0))
    svg = _figure(_CAPTION, _CATEGORY, number, *((beside,) if unit_beside_the_number else ()))
    # Without a unit span of its own the model cites the wrong occurrence and the qualifier
    # reads ``$m`` out of the figure's own axis caption, which is the AIA shape.
    unit = _field(svg, "$m") if unit_beside_the_number else TextField("$m", _evidence(svg, "1H26"))
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "VONB ($m)"),
        unit,
        Decimal(printed.replace(",", "")),
        printed,
    )
    axis = ChartAxis("axis-y", _field(svg, "VONB ($m)"), _field(svg, "VONB ($m)"), "linear")
    chart = _chart(svg, points=(point,), axes=(axis,))
    return _projected(svg, chart, "VONB ($m)")


def _percent() -> ChartContext:
    """A percentage bar: the figure prints the unit itself, so the display carries it."""
    svg = _figure(
        ("Expense Ratio", (100.0, 10.0, 180.0, 22.0)),
        ("1H26", (100.0, 80.0, 130.0, 92.0)),
        ("8.2%", (100.0, 40.0, 130.0, 52.0)),
    )
    point = _point(
        svg,
        "point-1h26",
        _field(svg, "1H26"),
        _field(svg, "Expense Ratio"),
        TextField("%", _evidence(svg, "%")),
        Decimal("8.2"),
        "8.2%",
    )
    chart = _chart(svg, points=(point,), title=_field(svg, "Expense Ratio"))
    return _projected(svg, chart, "Expense Ratio")


def _projected(svg: SvgArtifact, chart: ChartIR, label: str) -> ChartContext:
    projection = qualify_source_labels(svg, chart, _description(svg, _label(svg, label)))
    return ChartContext(
        PIN, "d" * 64, projection.chart, projection.description, projection.receipt, svg
    )


def _block() -> ContextBlock:
    return ContextBlock(
        PIN.snapshot_id,
        MEMBER,
        BlockKind.CHART,
        3,
        FIGURE_POINT_SCOPE,
        Verification.PENDING,
        "VONB ($m)",
    )


def _claim(
    field_path: str, text: str, *, claim_id: str = "c1", member_id: str = MEMBER
) -> ModelClaim:
    return ModelClaim(
        claim_id=claim_id,
        member_id=member_id,
        kind="chart_value",
        field_path=field_path,
        text=text,
        row=None,
        col=None,
        header=None,
    )


def _verify(context: ChartContext, *claims: ModelClaim) -> ClaimVerification:
    answer = ModelAnswer(abstain=False, abstain_reason=None, answer="", claims=claims)
    return verify_claims(answer, {MEMBER: _block()}, chart_evidence=lambda _: context)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("294", ("294", "")),
        ("294$m", ("294", "$m")),
        ("294 $m", ("294", "$m")),
        ("$294m", ("294", "$m")),
        ("$294 m", ("294", "$m")),
        ("8.2%", ("8.2", "%")),
        ("8.2 %", ("8.2", "%")),
        ("+17%", ("+17", "%")),
        ("-294 $m", ("-294", "$m")),
        ("(294)$m", ("(294)", "$m")),
        ("1,168 $m", ("1,168", "$m")),
        ("294 US cents", ("294", "US cents")),
        ("294.0$m", ("294.0", "$m")),
        ("no number at all", ("no number at all", "")),
    ],
)
def test_the_claim_splitter_keeps_the_number_exactly_as_written(
    text: str, expected: tuple[str, str]
) -> None:
    """Only the unit is taken off: no rounding, no separator dropped, no sign rewritten."""
    assert _split_unit(text) == expected


@pytest.mark.parametrize("text", ["294", "294$m", "294 $m", "$294m", "$294 m"])
def test_a_claim_may_carry_the_points_unit_around_a_bare_display(text: str) -> None:
    (claim,) = _verify(_money("294"), _claim(POINT, text)).verified

    assert (claim.kind, claim.text, claim.value, claim.unit) == (
        ClaimKind.CHART_VALUE,
        "294",
        Decimal("294"),
        "$m",
    )


def test_a_unit_printed_beside_the_number_is_still_split_off_the_claim() -> None:
    """``verbatim_display`` already joins that unit on; a claim spelling it differently passes."""
    context = _money("294", unit_beside_the_number=True)

    for claim_text in ("294 $m", "294$m", "$294m", "294"):
        (claim,) = _verify(context, _claim(POINT, claim_text)).verified
        assert claim.text == "294 $m" and claim.value == Decimal("294")


def test_a_thousands_separator_stays_part_of_the_display() -> None:
    (claim,) = _verify(_money("1,168"), _claim(POINT, "1,168 $m")).verified

    assert claim.text == "1,168" and claim.value == Decimal("1168")


def test_another_number_with_the_right_unit_is_still_not_in_evidence() -> None:
    (rejected,) = _verify(_money("294"), _claim(POINT, "295$m")).rejected

    assert rejected.reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
    assert "'295$m'" in rejected.detail and "'294'" in rejected.detail


def test_the_right_number_with_another_unit_is_a_unit_mismatch() -> None:
    """Never relax to "the number matches": the unit is half of what the figure stated."""
    (rejected,) = _verify(_money("294"), _claim(POINT, "294%")).rejected

    assert rejected.reason is AbstainReason.UNIT_MISMATCH
    assert "'%'" in rejected.detail and "'$m'" in rejected.detail


def test_a_point_printing_no_unit_refuses_a_claimed_one() -> None:
    context = _money("294")
    unitless = replace(
        context,
        chart=replace(
            context.chart,
            points=tuple(
                replace(point, unit=replace(point.unit, text="")) for point in context.chart.points
            ),
        ),
    )

    (claim,) = _verify(unitless, _claim(POINT, "294")).verified
    (rejected,) = _verify(unitless, _claim(POINT, "294$m")).rejected

    assert claim.text == "294" and claim.unit == ""
    assert rejected.reason is AbstainReason.UNIT_MISMATCH
    assert "no unit" in rejected.detail


@pytest.mark.parametrize("text", ["8.2%", "8.2 %", "8.2"])
def test_a_percentage_display_that_already_passed_keeps_passing(text: str) -> None:
    (claim,) = _verify(_percent(), _claim(POINT, text)).verified

    assert (claim.text, claim.value, claim.unit) == ("8.2%", Decimal("8.2"), "%")


def test_a_percentage_point_refuses_a_money_unit() -> None:
    (rejected,) = _verify(_percent(), _claim(POINT, "8.2$m")).rejected

    assert rejected.reason is AbstainReason.UNIT_MISMATCH


def test_the_displayed_bar_branch_proves_the_unit_by_the_same_rule(tmp_path: Path) -> None:
    """The three chart branches share ``_value_claim``; the displayed bar is checked alike."""
    document, pin = bar_document(tmp_path)
    hit = PinnedRetrievalHit(document.retrieval_snapshot_id, pin.member_id, 1.0)
    blocks = {pin.member_id: build_context_block(document.resolve(hit))}
    context = document.displayed_context(hit)
    model = ModelAnswer(
        abstain=False,
        abstain_reason=None,
        answer="",
        claims=(
            _claim("points.p-1H21.value", "15 %", claim_id="spaced", member_id=pin.member_id),
            _claim("points.p-1H21.value", "15$m", claim_id="unit", member_id=pin.member_id),
            _claim("points.p-1H21.value", "16%", claim_id="number", member_id=pin.member_id),
        ),
    )

    verification = verify_claims(model, blocks, chart_evidence=lambda _: context)

    (verified,) = verification.verified
    rejected = {claim.claim_id: claim for claim in verification.rejected}
    assert verified.claim_id == "spaced" and verified.text == "15%"
    assert rejected["unit"].reason is AbstainReason.UNIT_MISMATCH
    assert rejected["number"].reason is AbstainReason.CLAIM_NOT_IN_EVIDENCE
