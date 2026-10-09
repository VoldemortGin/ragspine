"""ADR 0038:模型写下的派生计算,由代码按四则文法复算后才可被 prose 引用。

求值器是手写的 tokenizer + 递归下降,只认 ``+ - * / ( )``、数字字面量与变量名,
从不使用 ``ast`` / ``eval`` / ``exec``。输入只能回指本次已验证的 claim(其 ``text``
里印着的数字)或调用方给定的常量;结果按其自身末位小数的半个单位做舍入容差。
"""

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Final

from enterprise_pdf_rag.answers.models import (
    DerivationFailure,
    DerivationOperand,
    DerivationVerification,
    RejectedDerivation,
    VerifiedClaim,
    VerifiedDerivation,
)
from enterprise_pdf_rag.answers.prompt import (
    DerivationInput,
    ModelAnswer,
    ModelAnswerWithDerivations,
    ModelDerivation,
)
from enterprise_pdf_rag.answers.verify import _NUMBER_RE, _decimal, _numbers

MAX_EXPRESSION_CHARS: Final = 200
MAX_TOKENS: Final = 64
MAX_DEPTH: Final = 16
_PRECISION: Final = 34
_RELATIVE_SLACK: Final = Decimal("1e-9")

_TOKEN_RE: Final = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?(?![\w.]))|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[-+*/()]))"
)
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ExpressionError(ValueError):
    """表达式无法求值;``code`` 为 invalid_expression / unknown_variable /
    division_by_zero / expression_too_long 之一。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _tokens(expression: str) -> list[tuple[str, str]]:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ExpressionError("expression_too_long")
    tokens: list[tuple[str, str]] = []
    position = 0
    end = len(expression.rstrip())
    while position < end:
        match = _TOKEN_RE.match(expression, position)
        if match is None or match.end() > end:
            raise ExpressionError("invalid_expression")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        if len(tokens) > MAX_TOKENS:
            raise ExpressionError("expression_too_long")
        position = match.end()
    return tokens


class _Parser:
    """expr := term (('+'|'-') term)*;term := unary (('*'|'/') unary)*;
    unary := ('+'|'-') unary | atom;atom := NUMBER | NAME | '(' expr ')'。"""

    def __init__(self, tokens: list[tuple[str, str]], variables: Mapping[str, Decimal]) -> None:
        self._tokens = tokens
        self._variables = variables
        self._index = 0

    def parse(self) -> Decimal:
        value = self._expr(0)
        if self._index != len(self._tokens):
            raise ExpressionError("invalid_expression")
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take_op(self, ops: str) -> str | None:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in ops:
            self._index += 1
            return token[1]
        return None

    def _expr(self, depth: int) -> Decimal:
        value = self._term(depth)
        while (op := self._take_op("+-")) is not None:
            right = self._term(depth)
            value = value + right if op == "+" else value - right
        return value

    def _term(self, depth: int) -> Decimal:
        value = self._unary(depth)
        while (op := self._take_op("*/")) is not None:
            right = self._unary(depth)
            if op == "*":
                value = value * right
            elif right == 0:
                raise ExpressionError("division_by_zero")
            else:
                value = value / right
        return value

    def _unary(self, depth: int) -> Decimal:
        if depth > MAX_DEPTH:
            raise ExpressionError("expression_too_long")
        op = self._take_op("+-")
        if op is not None:
            value = self._unary(depth + 1)
            return -value if op == "-" else +value
        return self._atom(depth)

    def _atom(self, depth: int) -> Decimal:
        token = self._peek()
        if token is None:
            raise ExpressionError("invalid_expression")
        kind, text = token
        self._index += 1
        if kind == "number":
            return Decimal(text)
        if kind == "name":
            if text not in self._variables:
                raise ExpressionError("unknown_variable")
            return self._variables[text]
        if text == "(":
            value = self._expr(depth + 1)
            if self._take_op(")") is None:
                raise ExpressionError("invalid_expression")
            return value
        raise ExpressionError("invalid_expression")


def evaluate(expression: str, variables: Mapping[str, Decimal]) -> Decimal:
    """按四则文法求值(34 位有效数字);任何越出文法的输入抛 ``ExpressionError``。"""
    tokens = _tokens(expression)
    with localcontext() as context:
        context.prec = _PRECISION
        return _Parser(tokens, variables).parse()


def _names(expression: str) -> set[str]:
    return {text for kind, text in _tokens(expression) if kind == "name"}


def _tolerance(written: Decimal) -> Decimal:
    """写法末位小数的半个单位,另加 1e-9 的相对项。"""
    exponent = written.as_tuple().exponent
    assert isinstance(exponent, int)
    return Decimal(5).scaleb(exponent - 1) + abs(written) * _RELATIVE_SLACK


def _number(text: str) -> Decimal | None:
    folded = text.strip()
    if _NUMBER_RE.fullmatch(folded) is None:
        return None
    return _decimal(folded)


class _Rejected(Exception):
    def __init__(self, reason: DerivationFailure, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _operand(
    item: DerivationInput,
    claims: Mapping[str, VerifiedClaim],
    constants: Mapping[str, float],
) -> DerivationOperand:
    if (item.claim_id is None) == (item.constant is None):
        raise _Rejected(DerivationFailure.INVALID_INPUT, f"{item.name}: name one source")
    value = _number(item.value)
    if value is None:
        raise _Rejected(DerivationFailure.INVALID_INPUT, f"{item.name}: value is not a number")
    if item.claim_id is not None:
        claim = claims.get(item.claim_id)
        if claim is None:
            raise _Rejected(
                DerivationFailure.UNVERIFIED_CLAIM, f"{item.name}: {item.claim_id} is not verified"
            )
        printed = {number for _, number in _numbers(claim.text)}
        if claim.value is not None:
            printed.add(claim.value)
        if value not in printed:
            raise _Rejected(
                DerivationFailure.INPUT_NOT_IN_CLAIM,
                f"{item.name}: {item.value} is not printed by {item.claim_id}",
            )
        return DerivationOperand(item.name, value, item.claim_id, None)
    assert item.constant is not None
    if item.constant not in constants:
        raise _Rejected(
            DerivationFailure.UNKNOWN_CONSTANT, f"{item.name}: unknown constant {item.constant}"
        )
    if value != Decimal(repr(float(constants[item.constant]))):
        raise _Rejected(
            DerivationFailure.CONSTANT_MISMATCH,
            f"{item.name}: {item.value} is not the value of {item.constant}",
        )
    return DerivationOperand(item.name, value, None, item.constant)


def _verify(
    derivation: ModelDerivation,
    claims: Mapping[str, VerifiedClaim],
    constants: Mapping[str, float],
) -> VerifiedDerivation:
    operands: list[DerivationOperand] = []
    for item in derivation.inputs:
        if _IDENTIFIER_RE.fullmatch(item.name) is None or any(
            operand.name == item.name for operand in operands
        ):
            raise _Rejected(
                DerivationFailure.INVALID_INPUT, f"input name {item.name!r} is invalid or repeated"
            )
        operands.append(_operand(item, claims, constants))
    variables = {operand.name: operand.value for operand in operands}
    try:
        if not _names(derivation.expression):
            # 只有字面量的式子不引用任何证据,等于凭空造数。
            raise _Rejected(DerivationFailure.INVALID_EXPRESSION, "expression uses no input")
        computed = evaluate(derivation.expression, variables)
    except ExpressionError as error:
        reason = (
            DerivationFailure.DIVISION_BY_ZERO
            if error.code == "division_by_zero"
            else DerivationFailure.INVALID_EXPRESSION
        )
        raise _Rejected(reason, error.code) from error
    result = _number(derivation.result)
    if result is None:
        raise _Rejected(DerivationFailure.INVALID_RESULT, "result is not one number")
    if abs(computed - result) > _tolerance(result):
        raise _Rejected(DerivationFailure.RESULT_MISMATCH, f"computed {computed}")
    return VerifiedDerivation(
        derivation.name, derivation.expression, tuple(operands), derivation.result, computed
    )


def verify_derivations(
    model: ModelAnswer,
    verified: Sequence[VerifiedClaim],
    constants: Mapping[str, float],
) -> DerivationVerification:
    """逐条复算模型的派生;基类 ``ModelAnswer``(未放开计算)恒返回空结果。

    顺序:名字合法且唯一 → 每个输入恰有一个来源 → claim 已验证且印着该值 → 常量在表内
    且值相等 → 求值 → result 可解析 → |computed - result| ≤ result 末位半个单位(+相对项)。
    被拒的 claim 不在 ``verified`` 里,因此永远不能作为输入。
    """
    if not isinstance(model, ModelAnswerWithDerivations):
        return DerivationVerification()
    claims = {claim.claim_id: claim for claim in verified}
    accepted: list[VerifiedDerivation] = []
    rejected: list[RejectedDerivation] = []
    seen: set[str] = set()
    for derivation in model.derivations:
        try:
            if _IDENTIFIER_RE.fullmatch(derivation.name) is None:
                raise _Rejected(DerivationFailure.INVALID_INPUT, "derivation name is invalid")
            if derivation.name in seen:
                raise _Rejected(DerivationFailure.DUPLICATE_NAME, "duplicate derivation name")
            seen.add(derivation.name)
            accepted.append(_verify(derivation, claims, constants))
        except _Rejected as failure:
            rejected.append(
                RejectedDerivation(
                    derivation.name,
                    derivation.expression,
                    derivation.result,
                    failure.reason,
                    failure.detail,
                )
            )
    return DerivationVerification(tuple(accepted), tuple(rejected))
