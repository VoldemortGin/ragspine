"""从 LLM 回文里容错提取 JSON 对象 / 数组：只依赖 stdlib，不抛异常，不写 trace。

``extract_json`` 先看全文：全文能严格解析成符合 ``expect`` 的容器就直接返回。否则分两轮：先对候选文本
严格解析，失败后才做修复。候选文本依次是各个代码围栏（```` ``` ````）里的内容和全文；全文带说明文字时，
JSON 字符串里的 ```` ``` ```` 仍可能被当成围栏边界（已知局限）。每轮在候选文本里找 ``{`` / ``[`` 起点，按字符串感知的括号配对
找到闭合位置，只取顶层整段：一段解析失败或类型不符就整段跳过，不往里面找；最多试
``_MAX_STARTS`` 个起点。

修复只有四种，且只作用于字符串字面量之外：去代码围栏；删掉紧挨在 ``]`` / ``}`` 前的尾逗号；
给值位置的裸标识符原样补引号（该段已有合法双引号字符串时才补，``true/null/None/NaN`` 等不补）；
从前后说明文字里提取第一个 JSON。不补截断、不转单引号、不补键的引号、不删注释、
不认 Python 字面量和 ``NaN`` / ``Infinity``、不处理连续或开头的逗号、不返回裸标量。
"""

import json
import re
from collections.abc import Callable
from typing import Any, Literal, NoReturn, TypeGuard

_MAX_STARTS = 64

_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n?(.*?)```", re.DOTALL)
_OPEN_RE = re.compile(r"[\[{]")
# 从起点往后扫描的记号：字符串字面量（未闭合时吞到文末）或括号。
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"?|[\[\]{}]', re.DOTALL)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_CLOSERS = {"[": "]", "{": "}"}
# 前面是一个值（不是 `[` `{` `,`）且后面紧跟 `]` / `}` 的逗号。
_TRAILING_COMMA_RE = re.compile(r"(?<=[^\s\[{,])(\s*),(?=\s*[\]}])")
# 前面是 `[` `,` `:`、后面是 `,` `]` `}` 的 ASCII 标识符。
_BARE_VALUE_RE = re.compile(r"(?<=[\[,:])(\s*)([A-Za-z_][A-Za-z0-9_]*)(?=\s*[,\]}])")
_BARE_KEYWORDS = frozenset({"true", "false", "null", "none", "nan", "infinity", "undefined"})

_Container = dict[str, Any] | list[Any]


def extract_json(
    text: str, *, expect: Literal["object", "array"] | None = None
) -> dict[str, Any] | list[Any] | None:
    """返回 ``text`` 里第一个类型符合 ``expect`` 的顶层 JSON 容器；找不到返回 ``None``，从不抛异常。"""
    found = _extract(text, expect)
    return None if found is None else found[0]


def parse_llm_json(text: str, *, expect: Literal["object", "array"] | None = None) -> Any:
    """调用点用的解析：先按原来的 ``json.loads(text.strip())`` 解析，成功就原样返回（任何类型，
    含 ``NaN`` / ``Infinity``），保证原来能解析的输入行为不变；失败才交给 ``extract_json``。"""
    try:
        return json.loads(text.strip())
    except (ValueError, RecursionError):
        return extract_json(text, expect=expect)


def _extract(
    text: str, expect: Literal["object", "array"] | None
) -> tuple[_Container, frozenset[str]] | None:
    """``extract_json`` 的实现，另外返回用到的修复（``fence`` / ``surrounding_text`` /
    ``trailing_comma`` / ``bare_value``），供测试观测。"""
    try:
        whole = json.loads(text.strip(), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        whole = None
    if _is_expected(whole, expect):
        return whole, frozenset()
    candidates = [(m.group(1), True) for m in _FENCE_RE.finditer(text)] + [(text, False)]
    spans = [(c, fenced, _top_level_spans(c)) for c, fenced in candidates]
    for repair in (False, True):
        for candidate, fenced, starts in spans:
            for start, end in starts:
                segment = candidate[start:end]
                fixes: set[str] = set()
                if repair:
                    segment, fixes = _repair(segment)
                    if not fixes:
                        continue
                try:
                    value = json.loads(segment, parse_constant=_reject_constant)
                except (ValueError, RecursionError):
                    continue
                if not _is_expected(value, expect):
                    continue
                if fenced:
                    fixes.add("fence")
                if candidate[start:end] != candidate.strip():
                    fixes.add("surrounding_text")
                return value, frozenset(fixes)
    return None


def _is_expected(value: Any, expect: Literal["object", "array"] | None) -> TypeGuard[_Container]:
    if isinstance(value, dict):
        return expect != "array"
    return isinstance(value, list) and expect != "object"


def _top_level_spans(text: str) -> list[tuple[int, int]]:
    """最多 ``_MAX_STARTS`` 个起点的闭合区间；已配对的整段内部不再作为起点。"""
    spans: list[tuple[int, int]] = []
    skip_to = 0
    tried = 0
    for m in _OPEN_RE.finditer(text):
        start = m.start()
        if start < skip_to:
            continue
        if tried == _MAX_STARTS:
            break
        tried += 1
        end = _span_end(text, start)
        if end is not None:
            spans.append((start, end))
            skip_to = end
    return spans


def _span_end(text: str, start: int) -> int | None:
    stack: list[str] = []
    for m in _TOKEN_RE.finditer(text, start):
        token = m.group()
        if token in _CLOSERS:
            stack.append(_CLOSERS[token])
        elif token in ("]", "}"):
            if stack.pop() != token:
                return None
            if not stack:
                return m.end()
    return None


def _repair(segment: str) -> tuple[str, set[str]]:
    fixes: set[str] = set()
    fixed = _sub_outside_strings(segment, _TRAILING_COMMA_RE, r"\1")
    if fixed != segment:
        fixes.add("trailing_comma")
    if _STRING_RE.search(fixed):
        quoted = _sub_outside_strings(fixed, _BARE_VALUE_RE, _quote_bare_value)
        if quoted != fixed:
            fixes.add("bare_value")
            fixed = quoted
    return fixed, fixes


def _sub_outside_strings(
    segment: str, pattern: re.Pattern[str], repl: str | Callable[[re.Match[str]], str]
) -> str:
    # 字符串之间的片段拼上前一个字符串的收尾引号再替换，lookbehind 才能看到跨片段的上文。
    out: list[str] = []
    pos = 0
    for m in _STRING_RE.finditer(segment):
        context = '"' if pos else ""
        out.append(pattern.sub(repl, context + segment[pos : m.start()])[len(context) :])
        out.append(m.group())
        pos = m.end()
    context = '"' if pos else ""
    out.append(pattern.sub(repl, context + segment[pos:])[len(context) :])
    return "".join(out)


def _quote_bare_value(m: re.Match[str]) -> str:
    if m.group(2).lower() in _BARE_KEYWORDS:
        return m.group()
    return f'{m.group(1)}"{m.group(2)}"'


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {name}")
