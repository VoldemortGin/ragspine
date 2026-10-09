"""``answer_system``: the answer stage's system text, optionally extended by the caller."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from enterprise_pdf_rag.answers.prompt import (
    _RULE_2,
    _RULE_2_DERIVED,
    _RULE_3,
    _RULE_3_DERIVED,
    MAX_DERIVATIONS,
    MAX_SYSTEM_CHARS,
    SYSTEM_RULES,
    SYSTEM_RULES_DERIVED,
    DerivationInput,
    ModelAnswer,
    ModelAnswerWithDerivations,
    ModelClaim,
    ModelDerivation,
    answer_system,
)


def test_no_extra_rules_leaves_the_system_text_byte_identical() -> None:
    # The system text is part of the model-cache fingerprint: the default must not move.
    assert answer_system() is SYSTEM_RULES
    assert answer_system(None) == SYSTEM_RULES
    assert answer_system("") == SYSTEM_RULES
    assert answer_system("  \n\t ") == SYSTEM_RULES


def test_extra_rules_are_appended_after_the_original_text() -> None:
    system = answer_system("  Always answer in English.\n")

    assert system.startswith(SYSTEM_RULES + "\nAdditional rules (")
    assert system.endswith("the additional rule prevails):\nAlways answer in English.")


def test_extra_rules_beyond_the_input_budget_fail_at_once() -> None:
    room = MAX_SYSTEM_CHARS - len(answer_system("x")) + 1
    assert len(answer_system("x" * room)) == MAX_SYSTEM_CHARS
    with pytest.raises(ValueError, match="answer_system_rules_too_long"):
        answer_system("x" * (room + 1))


# ``ModelAnswer`` 的 JSON schema 在实施 ADR 0038 之前算出并钉死:schema 参与请求指纹,
# 默认路径的 schema 一个字节都不能动。
_MODEL_ANSWER_SCHEMA_SHA256 = "fb0f34bd8e5ff513c57affaef860748a4d5d88c671dbc557a272297594b6fa9d"


def test_the_default_model_answer_schema_is_byte_identical() -> None:
    schema = json.dumps(ModelAnswer.model_json_schema(), sort_keys=True)
    assert hashlib.sha256(schema.encode()).hexdigest() == _MODEL_ANSWER_SCHEMA_SHA256


def test_the_derived_rules_replace_exactly_rules_two_and_three() -> None:
    assert SYSTEM_RULES.count(_RULE_2) == 1
    assert SYSTEM_RULES.count(_RULE_3) == 1
    assert (
        SYSTEM_RULES.replace(_RULE_2, _RULE_2_DERIVED).replace(_RULE_3, _RULE_3_DERIVED)
        == SYSTEM_RULES_DERIVED
    )
    assert _RULE_2 not in SYSTEM_RULES_DERIVED and _RULE_3 not in SYSTEM_RULES_DERIVED
    assert "is not a reason to abstain" in SYSTEM_RULES_DERIVED
    assert "derivations" in SYSTEM_RULES_DERIVED


def test_derivations_switch_the_base_and_constants_follow_the_rules() -> None:
    assert answer_system(derivations=True) == SYSTEM_RULES_DERIVED
    system = answer_system(
        "Answer in English.",
        derivations=True,
        constants={"hkd_per_usd_2024": 7.803, "hkd_per_usd_default": 7.80},
    )
    assert system.startswith(SYSTEM_RULES_DERIVED + "\nConstants (")
    assert "\nhkd_per_usd_2024 = 7.803\nhkd_per_usd_default = 7.8\n" in system
    assert system.endswith("the additional rule prevails):\nAnswer in English.")


def test_constants_are_printed_in_name_order_whatever_the_mapping_order() -> None:
    one = answer_system(derivations=True, constants={"b_rate": 2.0, "a_rate": 1.5})
    two = answer_system(derivations=True, constants={"a_rate": 1.5, "b_rate": 2.0})
    assert one == two and one.index("a_rate = 1.5") < one.index("b_rate = 2.0")


def test_constants_need_derivations_and_a_lowercase_name() -> None:
    with pytest.raises(ValueError, match="answer_constants_need_derivations"):
        answer_system(constants={"hkd_per_usd_default": 7.8})
    for name in ("HKD", "1rate", "rate-x", ""):
        with pytest.raises(ValueError, match="invalid_answer_constant_name"):
            answer_system(derivations=True, constants={name: 7.8})
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="invalid_answer_constant_value"):
            answer_system(derivations=True, constants={"rate": value})


def test_the_derived_system_is_bounded_like_the_default_one() -> None:
    room = MAX_SYSTEM_CHARS - len(answer_system("x", derivations=True)) + 1
    assert len(answer_system("x" * room, derivations=True)) == MAX_SYSTEM_CHARS
    with pytest.raises(ValueError, match="answer_system_rules_too_long"):
        answer_system("x" * (room + 1), derivations=True)


def test_the_derivation_schema_extends_the_answer_schema_only() -> None:
    properties = ModelAnswerWithDerivations.model_json_schema()["properties"]
    assert list(properties) == [*ModelAnswer.model_json_schema()["properties"], "derivations"]
    claim = ModelClaim(
        claim_id="c1", member_id="m1", kind="cell", field_path="cells.x", text="1,234"
    )
    derivation = ModelDerivation(
        name="usd",
        expression="hkd / rate",
        inputs=(
            DerivationInput(name="hkd", claim_id="c1", value="1,234"),
            DerivationInput(name="rate", constant="hkd_per_usd_default", value="7.8"),
        ),
        result="158.21",
    )
    model = ModelAnswerWithDerivations(
        abstain=False,
        abstain_reason=None,
        answer="US$158.21",
        claims=(claim,),
        derivations=(derivation,),
    )
    assert ModelAnswerWithDerivations.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        ModelAnswerWithDerivations(
            abstain=False,
            abstain_reason=None,
            answer="",
            claims=(),
            derivations=(derivation,) * (MAX_DERIVATIONS + 1),
        )
