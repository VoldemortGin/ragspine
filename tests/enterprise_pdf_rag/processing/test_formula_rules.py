"""Formula proofs quote spans and paths; anything unproven withholds the whole formula."""

import pytest

from ragspine.extraction.evidence.document.models import Bounds
from ragspine.extraction.evidence.figures.models import Verification
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaSourceObservation,
    FormulaToken,
    ObservedChar,
    ObservedPath,
    ObservedRun,
    ScriptEvidence,
    ScriptPosition,
    StructureKind,
    TokenRole,
)
from ragspine.extraction.evidence.objects.formulas.formula_rules import (
    check_formula,
    check_tiling,
    linearize,
    readable_text,
    rise_of,
    script_of,
    tile_run,
)
from ragspine.extraction.evidence.objects.typed_ir import FormulaIR
from tests.enterprise_pdf_rag.processing.formula_observation_fixtures import (
    PAGE_HEIGHT,
    anchor,
    line,
    observation,
    run,
)

FRACTION_BBOX: Bounds = (18.0, 56.0, 130.0, 100.0)
POWER_BBOX: Bounds = (150.0, 62.0, 172.0, 86.0)
RADICAL_BBOX: Bounds = (56.0, 66.0, 94.0, 88.0)

# `ROE = Net profit / Equity`, with the rule drawn between the two operands.
FRACTION_RUNS = (
    run("span-roe", "ROE =", origin=(20.0, 82.0), size=12.0),
    run("span-num", "Net profit", origin=(62.0, 74.0), size=11.0),
    run("span-den", "Equity", origin=(72.0, 94.0), size=11.0),
)
FRACTION_RULE = line(0, 78.0, 60.0, 120.0)
BASE_X = run("span-x", "x", origin=(152.0, 82.0), size=12.0)


def _check(
    runs: tuple[ObservedRun, ...], paths: tuple[ObservedPath, ...], *, bbox: Bounds
) -> tuple[FormulaIR | None, tuple[str, ...]]:
    source = observation(runs, paths, bbox=bbox)
    result = check_formula(source, object_id="obj-formula", anchor=anchor(bbox))
    return result.ir, result.diagnostics


def _proven(source: FormulaSourceObservation) -> FormulaIR:
    result = check_formula(source, object_id="obj-formula", anchor=anchor(source.bbox))
    assert result.ir is not None, result.diagnostics
    return result.ir


def _token(
    index: int,
    text: str,
    *,
    bbox: Bounds,
    script: ScriptPosition = ScriptPosition.BASE,
    base_token_index: int | None = None,
) -> FormulaToken:
    is_base = script is ScriptPosition.BASE
    return FormulaToken(
        index,
        text,
        "span-x",
        0,
        len(text),
        bbox,
        TokenRole.OPERAND,
        script,
        None if is_base else "derived",
        base_token_index,
        None if is_base else ScriptEvidence(None, 0.5, 6.0, False),
    )


def test_tiling_splits_a_merged_span_and_closes_verbatim() -> None:
    pieces = tile_run("ROE =")

    assert pieces == ((0, 3, TokenRole.OPERAND), (4, 5, TokenRole.RELATION))
    assert check_tiling("ROE =", [(start, end) for start, end, _ in pieces]) is None


def test_tiling_rejects_a_gap_that_is_not_whitespace() -> None:
    assert check_tiling("ROE=", [(0, 3)]) == "formula_span_not_tiled:'='"


def test_tiling_rejects_overlapping_or_disordered_pieces() -> None:
    assert check_tiling("ROE =", [(0, 3), (2, 5)]) == "formula_span_pieces_overlap_or_disordered"
    assert check_tiling("ROE =", [(4, 5), (0, 3)]) != "formula_span_pieces_overlap_or_disordered"


def test_number_run_keeps_thousands_separator_and_decimal_point() -> None:
    assert tile_run("1,234.5%") == ((0, 7, TokenRole.NUMBER), (7, 8, TokenRole.UNIT))


def test_greek_and_operator_roles() -> None:
    assert tile_run("α+β") == (  # noqa: RUF001 — Greek and typographic operator glyphs are the subject of this case
        (0, 1, TokenRole.GREEK),
        (1, 2, TokenRole.OPERATOR),
        (2, 3, TokenRole.GREEK),
    )


def test_text_rise_superscript_is_proven() -> None:
    script = run("span-2", "2", origin=(159.2, 77.0), size=7.0, rise=5.0, flags=1)
    ir, diagnostics = _check((BASE_X, script), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    token = ir.tokens[1]
    assert token.script is ScriptPosition.SUPERSCRIPT
    assert token.script_proof == "text_rise"
    assert token.base_token_index == 0
    assert token.script_evidence is not None
    assert token.script_evidence.rise == 5.0


def test_text_rise_subscript_is_proven() -> None:
    script = run("span-i", "i", origin=(159.2, 85.0), size=7.0, rise=-3.0)
    ir, diagnostics = _check((BASE_X, script), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    token = ir.tokens[1]
    assert token.script is ScriptPosition.SUBSCRIPT
    assert token.script_proof == "text_rise"
    assert token.script_evidence is not None
    assert token.script_evidence.rise == -3.0


def test_text_rise_without_a_base_withholds() -> None:
    orphan = run("span-2", "2", origin=(160.0, 76.0), size=7.0, rise=5.0)
    ir, diagnostics = _check((orphan,), (), bbox=POWER_BBOX)

    assert ir is None
    assert "formula_script_without_base:span-2" in diagnostics


def test_non_identity_ctm_disables_rise_but_allows_derived() -> None:
    script = run(
        "span-2",
        "2",
        origin=(160.0, 76.0),
        size=7.0,
        ctm=(1.0, 0.0, 0.0, -1.0, 0.0, 160.0),
    )
    assert rise_of(script, PAGE_HEIGHT) is None

    ir, diagnostics = _check((BASE_X, script), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    token = ir.tokens[1]
    assert token.script is ScriptPosition.SUPERSCRIPT
    assert token.script_proof == "derived"
    assert token.script_evidence is not None
    assert token.script_evidence.rise is None


def test_small_raised_run_is_derived_superscript() -> None:
    script = run("span-2", "2", origin=(160.0, 76.0), size=7.0, flags=1)
    ir, diagnostics = _check((BASE_X, script), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    token = ir.tokens[1]
    assert token.script is ScriptPosition.SUPERSCRIPT
    assert token.script_proof == "derived"
    evidence = token.script_evidence
    assert evidence is not None
    assert (evidence.size_ratio, evidence.baseline_offset) == (7.0 / 12.0, 6.0)
    assert evidence.superscript_flag is True


def test_small_lowered_run_is_derived_subscript() -> None:
    script = run("span-i", "i", origin=(160.0, 84.0), size=7.0)
    ir, diagnostics = _check((BASE_X, script), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    token = ir.tokens[1]
    assert token.script is ScriptPosition.SUBSCRIPT
    assert token.script_proof == "derived"
    assert token.script_evidence is not None
    assert token.script_evidence.baseline_offset == -2.0


def test_same_size_raised_run_stays_base() -> None:
    base = run("span-a", "A", origin=(50.0, 100.0), size=12.0)
    raised = run("span-b", "B", origin=(58.0, 94.0), size=12.0, flags=1)
    base_token = _token(0, "A", bbox=base.bbox)

    position, proof, evidence, refusal = script_of(raised, base_token, base, PAGE_HEIGHT)

    assert (position, proof, evidence, refusal) == (ScriptPosition.BASE, None, None, None)


def test_merged_same_size_span_x2_yields_two_base_tokens() -> None:
    merged = run("span-x2", "x2", origin=(152.0, 82.0), size=12.0)
    ir, diagnostics = _check((merged,), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    assert tuple(token.script for token in ir.tokens) == (
        ScriptPosition.BASE,
        ScriptPosition.BASE,
    )
    assert tuple(token.text for token in ir.tokens) == ("x", "2")
    assert ir.linear == "x 2"


def test_script_pair_shares_one_base_token() -> None:
    subscript = run("span-i", "i", origin=(160.0, 84.0), size=7.0)
    superscript = run("span-2", "2", origin=(164.2, 76.0), size=7.0)
    ir, diagnostics = _check((BASE_X, subscript, superscript), (), bbox=POWER_BBOX)

    assert diagnostics == ()
    assert ir is not None
    assert tuple(token.base_token_index for token in ir.tokens) == (None, 0, 0)
    assert tuple(token.script for token in ir.tokens) == (
        ScriptPosition.BASE,
        ScriptPosition.SUBSCRIPT,
        ScriptPosition.SUPERSCRIPT,
    )
    assert ir.linear == "x_{i}^{2}"


def test_fraction_rule_pairs_numerator_and_denominator() -> None:
    ir, diagnostics = _check(FRACTION_RUNS, (FRACTION_RULE,), bbox=FRACTION_BBOX)

    assert diagnostics == ()
    assert ir is not None
    assert len(ir.structures) == 1
    structure = ir.structures[0]
    assert structure.kind is StructureKind.FRACTION
    assert (structure.first, structure.second) == ((2, 3), (4,))
    assert structure.path.path_index == 0
    assert structure.path.kind == "line"
    assert structure.path.points == ((60.0, 78.0), (120.0, 78.0))


def test_fraction_rule_missing_withholds_structure_and_paths_must_be_explained() -> None:
    ir, diagnostics = _check(FRACTION_RUNS, (), bbox=FRACTION_BBOX)

    assert ir is None
    assert "formula_multiline_unsupported" in diagnostics
    # Without a rule there is no structure, so every token stays a top-level item in x order.
    proven = _proven(observation(FRACTION_RUNS, (FRACTION_RULE,), bbox=FRACTION_BBOX))
    assert linearize(proven.tokens, ()) == "ROE = Net Equity profit"


def test_fraction_rule_above_both_operands_is_unpaired() -> None:
    ir, diagnostics = _check(FRACTION_RUNS, (line(0, 60.0, 60.0, 120.0),), bbox=FRACTION_BBOX)

    assert ir is None
    assert "formula_fraction_line_unpaired:0" in diagnostics


def test_rule_too_thick_or_sloped_is_unexplained() -> None:
    ir, diagnostics = _check(
        FRACTION_RUNS, (line(0, 78.0, 60.0, 120.0, width=3.0),), bbox=FRACTION_BBOX
    )

    assert ir is None
    assert "formula_unexplained_path:0" in diagnostics


def test_filled_triangle_in_bbox_is_unexplained() -> None:
    triangle = ObservedPath(
        0,
        "f",
        1.0,
        True,
        (
            ("l", ((100.0, 90.0), (90.0, 95.0))),
            ("l", ((90.0, 95.0), (90.0, 85.0))),
            ("l", ((90.0, 85.0), (100.0, 90.0))),
        ),
    )
    ir, diagnostics = _check((BASE_X,), (triangle,), bbox=POWER_BBOX)

    assert ir is None
    assert diagnostics == ("formula_unexplained_path:0",)


def test_sqrt_glyph_with_overline() -> None:
    radical = run("span-radical", "√", origin=(60.0, 82.0), size=12.0)
    radicand = run("span-x", "x", origin=(69.0, 82.0), size=12.0)
    ir, diagnostics = _check((radical, radicand), (line(0, 72.0, 67.5, 85.0),), bbox=RADICAL_BBOX)

    assert diagnostics == ()
    assert ir is not None
    assert ir.tokens[0].role is TokenRole.RADICAL
    structure = ir.structures[0]
    assert structure.kind is StructureKind.SQRT
    assert (structure.first, structure.second) == ((1,), ())
    assert structure.radical_token_index == 0
    assert ir.linear == "\\sqrt{x}"
    assert ir.readable == "x 的平方根"


def test_sqrt_glyph_without_overline_withholds() -> None:
    radical = run("span-radical", "√", origin=(60.0, 82.0), size=12.0)
    radicand = run("span-x", "x", origin=(69.0, 82.0), size=12.0)
    ir, diagnostics = _check((radical, radicand), (), bbox=RADICAL_BBOX)

    assert ir is None
    assert diagnostics == ("formula_radical_without_overline:span-radical",)


def test_sqrt_drawn_as_polyline() -> None:
    drawn = ObservedPath(
        0,
        "s",
        1.0,
        False,
        (
            ("l", ((58.0, 78.0), (61.0, 84.0))),
            ("l", ((61.0, 84.0), (65.0, 70.0))),
            ("l", ((65.0, 70.0), (90.0, 70.0))),
        ),
    )
    radicand = run("span-x", "x", origin=(69.0, 82.0), size=12.0)
    ir, diagnostics = _check((radicand,), (drawn,), bbox=RADICAL_BBOX)

    assert diagnostics == ()
    assert ir is not None
    structure = ir.structures[0]
    assert structure.kind is StructureKind.SQRT
    assert structure.radical_token_index is None
    assert structure.path.kind == "polyline"
    assert structure.path.path_index == 0
    assert ir.linear == "\\sqrt{x}"


def test_token_in_two_structures_withholds() -> None:
    runs = (
        run("span-num", "Net", origin=(62.0, 74.0), size=11.0),
        run("span-mid", "Mid", origin=(70.0, 86.0), size=8.0),
        run("span-bot", "Bot", origin=(70.0, 98.0), size=8.0),
    )
    paths = (line(0, 78.0, 60.0, 120.0), line(1, 88.0, 60.0, 120.0))
    ir, diagnostics = _check(runs, paths, bbox=(18.0, 56.0, 130.0, 110.0))

    assert ir is None
    assert "formula_token_in_two_structures:0" in diagnostics


def test_linearize_latex_subset_keeps_symbols_verbatim() -> None:
    fraction = _proven(observation(FRACTION_RUNS, (FRACTION_RULE,), bbox=FRACTION_BBOX))
    assert fraction.linear == "ROE = \\frac{Net\\ profit}{Equity}"

    symbols = _proven(
        observation(
            (run("span-greek", "α×β", origin=(20.0, 82.0), size=12.0),),  # noqa: RUF001 — Greek and typographic operator glyphs are the subject of this case
            (),
            bbox=(18.0, 70.0, 44.0, 86.0),
        )
    )
    assert symbols.linear == "α × β"  # noqa: RUF001 — Greek and typographic operator glyphs are the subject of this case
    assert "\\alpha" not in symbols.linear
    assert "\\times" not in symbols.linear


def test_readable_wording_distinguishes_proven_and_derived_superscript() -> None:
    proven = _proven(
        observation(
            (BASE_X, run("span-2", "2", origin=(159.2, 77.0), size=7.0, rise=5.0)),
            (),
            bbox=POWER_BBOX,
        )
    )
    derived = _proven(
        observation(
            (BASE_X, run("span-2", "2", origin=(160.0, 76.0), size=7.0)), (), bbox=POWER_BBOX
        )
    )
    lowered = _proven(
        observation(
            (BASE_X, run("span-i", "i", origin=(160.0, 84.0), size=7.0)), (), bbox=POWER_BBOX
        )
    )

    assert proven.readable == "x 的 2 次方"
    assert derived.readable == "x 上标 2"
    assert lowered.readable == "x 下标 i"
    assert readable_text(proven.tokens, proven.structures) == proven.readable


def test_check_formula_full_and_literal_levels() -> None:
    full = _proven(observation(FRACTION_RUNS, (FRACTION_RULE,), bbox=FRACTION_BBOX))

    assert full.proof_level == "full"
    assert full.verification is Verification.VERIFIED
    assert full.latex is None
    assert full.source_literal == "ROE =\nNet profit\nEquity"
    assert full.source_span_ids == ("span-roe", "span-num", "span-den")
    assert full.readable == "ROE 等于 Net profit 除以 Equity"
    assert full.diagnostics == (
        "Tokens quote span substrings verbatim; structures quote get_cdrawings paths; "
        "derived scripts are typographic inferences",
        "proof_level=full",
    )

    literal = _proven(
        observation(
            (*FRACTION_RUNS, BASE_X, run("span-2", "2", origin=(160.0, 76.0), size=7.0)),
            (FRACTION_RULE,),
            bbox=(18.0, 56.0, 172.0, 100.0),
        )
    )

    assert literal.proof_level == "literal"
    assert literal.verification is Verification.PENDING
    assert literal.diagnostics[1] == "proof_level=literal"
    assert literal.linear == "ROE = \\frac{Net\\ profit}{Equity} x^{2}"
    assert literal.readable == "ROE 等于 Net profit 除以 Equity x 上标 2"


def test_check_formula_is_deterministic() -> None:
    source = observation(FRACTION_RUNS, (FRACTION_RULE,), bbox=FRACTION_BBOX)
    first = check_formula(source, object_id="obj-formula", anchor=anchor(FRACTION_BBOX))
    second = check_formula(source, object_id="obj-formula", anchor=anchor(FRACTION_BBOX))

    assert first == second


def test_formula_ir_post_init_rules() -> None:
    source = anchor(POWER_BBOX)
    base = _token(0, "x", bbox=(152.0, 72.4, 159.2, 84.4))

    with pytest.raises(ValueError, match="linear and readable"):
        FormulaIR(
            "obj",
            source,
            "x",
            None,
            ("span-x",),
            (),
            Verification.PENDING,
            (base,),
            (),
            None,
            None,
            "full",
        )
    with pytest.raises(ValueError, match="fully proven"):
        FormulaIR(
            "obj",
            source,
            "x",
            None,
            ("span-x",),
            (),
            Verification.VERIFIED,
            (base,),
            (),
            "x",
            "x",
            "literal",
        )
    scripts = (
        _token(
            0,
            "a",
            bbox=(1.0, 1.0, 2.0, 2.0),
            script=ScriptPosition.SUPERSCRIPT,
            base_token_index=1,
        ),
        _token(
            1,
            "b",
            bbox=(2.0, 1.0, 3.0, 2.0),
            script=ScriptPosition.SUPERSCRIPT,
            base_token_index=0,
        ),
    )
    with pytest.raises(ValueError, match="existing base token"):
        FormulaIR(
            "obj",
            source,
            "ab",
            None,
            ("span-x",),
            (),
            Verification.PENDING,
            scripts,
            (),
            "a b",
            "a b",
            "literal",
        )


def test_char_count_mismatch_withholds_the_formula() -> None:
    truncated = run(
        "span-roe",
        "ROE =",
        origin=(20.0, 82.0),
        size=12.0,
        chars=(ObservedChar("R", (20.0, 72.4, 27.2, 84.4)),),
    )
    ir, diagnostics = _check((truncated,), (), bbox=FRACTION_BBOX)

    assert ir is None
    assert "formula_char_count_mismatch:span-roe" in diagnostics
