"""ADR 0038:模型写下的派生计算由手写求值器复算,输入必须回指已验证 claim 或白名单常量。"""

from decimal import Decimal

import pytest

from enterprise_pdf_rag.answers.derivations import (
    ExpressionError,
    evaluate,
    verify_derivations,
)
from enterprise_pdf_rag.answers.models import ClaimKind, DerivationFailure, VerifiedClaim
from enterprise_pdf_rag.answers.prompt import (
    DerivationInput,
    ModelAnswer,
    ModelAnswerWithDerivations,
    ModelDerivation,
)

# 1234 / 7.8,34 位有效数字。
_USD = Decimal("158.2051282051282051282051282051282")
_VARS = {"a": Decimal("10"), "b": Decimal("4"), "rate_2024": Decimal("7.803")}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("a + b", Decimal("14")),
        ("a - b * 2", Decimal("2")),
        ("(a - b) * 2", Decimal("12")),
        ("(a - b) / b * 100", Decimal("150")),
        ("-a + +b", Decimal("-6")),
        ("--a", Decimal("10")),
        ("a / b", Decimal("2.5")),
        ("1234 / rate_2024", Decimal("158.1443034730231962065872100474177")),
        ("  12.5*2 ", Decimal("25.0")),
    ],
)
def test_evaluate_computes_the_four_operations(expression: str, expected: Decimal) -> None:
    assert evaluate(expression, _VARS) == expected


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("a ** 2", "invalid_expression"),
        ("__import__('os')", "invalid_expression"),
        ("__import__", "unknown_variable"),
        ("a if b else a", "invalid_expression"),
        ("1e5", "invalid_expression"),
        ("1_000", "invalid_expression"),
        ("", "invalid_expression"),
        ("a +", "invalid_expression"),
        ("(a + b", "invalid_expression"),
        ("a + b)", "invalid_expression"),
        ("a b", "invalid_expression"),
        (".5", "invalid_expression"),
        ("5.", "invalid_expression"),
        ("a % b", "invalid_expression"),
        ("c + 1", "unknown_variable"),
        ("a / (b - 4)", "division_by_zero"),
        ("0 / 0", "division_by_zero"),
        ("a" + " + a" * 100, "expression_too_long"),
        ("1+" * 32 + "1", "expression_too_long"),
        ("(" * 17 + "1" + ")" * 17, "expression_too_long"),
    ],
)
def test_evaluate_refuses_everything_outside_the_grammar(expression: str, code: str) -> None:
    with pytest.raises(ExpressionError) as raised:
        evaluate(expression, _VARS)
    assert raised.value.code == code


def test_evaluate_keeps_34_significant_digits() -> None:
    third = evaluate("1 / 3", {})
    assert third == Decimal("0." + "3" * 34)
    assert evaluate("(" * 16 + "1" + ")" * 16, {}) == Decimal("1")


def _claim(claim_id: str, text: str, value: Decimal | None = None) -> VerifiedClaim:
    return VerifiedClaim(claim_id, ClaimKind.CELL, text, value, None, ())


_CLAIMS = (_claim("c1", "1,234"), _claim("c2", "12%"), _claim("c3", "15%", Decimal("15")))
_CONSTANTS = {"hkd_per_usd_default": 7.8, "hkd_per_usd_2024": 7.803}


def _derivation(
    *inputs: DerivationInput,
    expression: str = "hkd / rate",
    result: str = "158.21",
    name: str = "usd",
) -> ModelDerivation:
    return ModelDerivation(name=name, expression=expression, inputs=inputs, result=result)


_HKD = DerivationInput(name="hkd", claim_id="c1", value="1,234")
_RATE = DerivationInput(name="rate", constant="hkd_per_usd_default", value="7.80")


def _model(*derivations: ModelDerivation) -> ModelAnswerWithDerivations:
    return ModelAnswerWithDerivations(
        abstain=False, abstain_reason=None, answer="", claims=(), derivations=derivations
    )


def _failure(derivation: ModelDerivation) -> DerivationFailure:
    outcome = verify_derivations(_model(derivation), _CLAIMS, _CONSTANTS)
    assert outcome.verified == ()
    (rejected,) = outcome.rejected
    return rejected.reason


def test_a_currency_conversion_from_a_claim_and_a_constant_is_verified() -> None:
    outcome = verify_derivations(_model(_derivation(_HKD, _RATE)), _CLAIMS, _CONSTANTS)
    assert outcome.rejected == ()
    (verified,) = outcome.verified
    assert verified.name == "usd" and verified.result == "158.21"
    assert verified.computed == _USD
    assert [(item.name, item.value, item.claim_id, item.constant) for item in verified.inputs] == [
        ("hkd", Decimal("1234"), "c1", None),
        ("rate", Decimal("7.80"), None, "hkd_per_usd_default"),
    ]


def test_a_percentage_change_rounds_to_its_own_last_decimal() -> None:
    old = DerivationInput(name="old", claim_id="c2", value="12")
    new = DerivationInput(name="new", claim_id="c3", value="15%")
    growth = _derivation(old, new, name="growth", expression="(new - old) / old * 100")
    for result in ("25", "25.0", "25.00"):
        derivation = growth.model_copy(update={"result": result})
        assert verify_derivations(_model(derivation), _CLAIMS, _CONSTANTS).rejected == ()
    third = _derivation(
        DerivationInput(name="x", claim_id="c3", value="15"),
        name="share",
        expression="x / 45 * 100",
        result="33.33",
    )
    assert verify_derivations(_model(third), _CLAIMS, _CONSTANTS).rejected == ()
    assert (
        _failure(third.model_copy(update={"result": "33.34"})) is DerivationFailure.RESULT_MISMATCH
    )
    # 少一位小数时容差随之放宽到 ±0.05:33.3 仍是 33.333… 的合法舍入。
    coarser = third.model_copy(update={"result": "33.3"})
    assert verify_derivations(_model(coarser), _CLAIMS, _CONSTANTS).rejected == ()


def test_a_result_off_by_more_than_half_its_last_unit_is_rejected() -> None:
    assert _failure(_derivation(_HKD, _RATE, result="158.22")) is DerivationFailure.RESULT_MISMATCH
    assert _failure(_derivation(_HKD, _RATE, result="159")) is DerivationFailure.RESULT_MISMATCH


@pytest.mark.parametrize("result", ["", "about 158", "1.58e2", "<UNAVAILABLE>"])
def test_a_result_that_is_not_one_number_is_invalid(result: str) -> None:
    assert _failure(_derivation(_HKD, _RATE, result=result)) is DerivationFailure.INVALID_RESULT


def test_an_unknown_or_misquoted_constant_is_rejected() -> None:
    unknown = DerivationInput(name="rate", constant="hkd_per_usd_1999", value="7.8")
    assert _failure(_derivation(_HKD, unknown)) is DerivationFailure.UNKNOWN_CONSTANT
    wrong = DerivationInput(name="rate", constant="hkd_per_usd_2024", value="7.8")
    assert _failure(_derivation(_HKD, wrong)) is DerivationFailure.CONSTANT_MISMATCH


def test_inputs_must_cite_a_verified_claim_and_a_number_it_prints() -> None:
    rejected = DerivationInput(name="hkd", claim_id="c9", value="1,234")
    assert _failure(_derivation(rejected, _RATE)) is DerivationFailure.UNVERIFIED_CLAIM
    invented = DerivationInput(name="hkd", claim_id="c1", value="1,235")
    assert _failure(_derivation(invented, _RATE)) is DerivationFailure.INPUT_NOT_IN_CLAIM
    unavailable = DerivationInput(name="hkd", claim_id="c1", value="<UNAVAILABLE>")
    assert _failure(_derivation(unavailable, _RATE)) is DerivationFailure.INVALID_INPUT
    by_value = DerivationInput(name="x", claim_id="c3", value="15")
    assert (
        verify_derivations(
            _model(_derivation(by_value, expression="x * 2", result="30")), _CLAIMS, _CONSTANTS
        ).rejected
        == ()
    )


@pytest.mark.parametrize(
    "operand",
    [
        DerivationInput(name="hkd", value="1,234"),
        DerivationInput(name="hkd", claim_id="c1", constant="hkd_per_usd_default", value="1,234"),
    ],
)
def test_each_input_names_exactly_one_source(operand: DerivationInput) -> None:
    assert _failure(_derivation(operand, _RATE)) is DerivationFailure.INVALID_INPUT


def test_input_names_are_identifiers_used_once() -> None:
    twice = DerivationInput(name="hkd", constant="hkd_per_usd_default", value="7.8")
    assert _failure(_derivation(_HKD, twice)) is DerivationFailure.INVALID_INPUT
    bad = DerivationInput(name="hkd value", claim_id="c1", value="1,234")
    assert _failure(_derivation(bad, _RATE)) is DerivationFailure.INVALID_INPUT


def test_expressions_must_parse_reference_inputs_and_divide_by_non_zero() -> None:
    assert _failure(_derivation(_HKD, _RATE, expression="hkd ** rate")) is (
        DerivationFailure.INVALID_EXPRESSION
    )
    assert _failure(_derivation(_HKD, _RATE, expression="hkd / other")) is (
        DerivationFailure.INVALID_EXPRESSION
    )
    assert _failure(_derivation(_HKD, _RATE, expression="hkd / (rate - 7.8)")) is (
        DerivationFailure.DIVISION_BY_ZERO
    )
    # 只有字面量、不引用任何输入的式子会凭空造数,一律拒绝。
    assert _failure(_derivation(_HKD, _RATE, expression="158.21")) is (
        DerivationFailure.INVALID_EXPRESSION
    )


def test_derivation_names_are_unique() -> None:
    first = _derivation(_HKD, _RATE)
    outcome = verify_derivations(_model(first, first), _CLAIMS, _CONSTANTS)
    assert [item.name for item in outcome.verified] == ["usd"]
    (duplicate,) = outcome.rejected
    assert duplicate.reason is DerivationFailure.DUPLICATE_NAME


def test_the_base_answer_model_has_no_derivations_to_verify() -> None:
    model = ModelAnswer(abstain=False, abstain_reason=None, answer="", claims=())
    outcome = verify_derivations(model, _CLAIMS, _CONSTANTS)
    assert outcome.verified == () and outcome.rejected == ()
