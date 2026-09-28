"""共享 LLM 回文 JSON 容错解析 ``extract_json`` 单测（TDD）。

钉死的契约：
- 只返回 JSON 容器（对象 / 数组）或 ``None``，任何输入都不抛异常。
- 两轮解析：先严格解析，失败后再修复。修复只有四种：去代码围栏、去 ``]`` / ``}`` 前的尾逗号、
  给值位置的裸标识符原样补引号、从前后说明文字里提取第一个顶层 JSON。修复只作用于字符串字面量之外。
- 不做的修复（截断补全、单引号、无引号键、注释、Python 字面量、NaN、连续逗号……）一律返回 ``None``。
- 只取顶层值：类型不符或解析失败的整段不往里面找。
- 嵌套深度超过 ``MAX_JSON_DEPTH`` 一律返回 ``None``（解析前扫描判定，不依赖平台栈深 / RecursionError）。
- 只依赖 stdlib，不写 trace。
"""

import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import rootutils
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common import llm_json
from ragspine.common.llm_json import extract_json, parse_llm_json

# 病态输入的耗时上限：原型实测 0.14s / 1.8s，放宽到 5s 防慢机误报。
_TIME_LIMIT_S = 5.0


def _fixes(text: str, expect: Any = None) -> frozenset[str]:
    found = llm_json._extract(text, expect)
    assert found is not None, text
    return found[1]


# ---------------------------------------------------------------------------
# 严格路径：合法 JSON 不经任何修复
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('  ["x", "y"]\n', ["x", "y"]),
        ("[]", []),
        ('{"a": [1, {"b": null}], "c": true}', {"a": [1, {"b": None}], "c": True}),
    ],
)
def test_strict_json_parses_without_fixes(text, expected):
    assert extract_json(text) == expected
    assert _fixes(text) == frozenset()


# ---------------------------------------------------------------------------
# 每条修复路径各自被触发、结果符合预期
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '```json\n["a", "b"]\n```',
        '```\n["a", "b"]\n```',
        '```JSON  \n["a", "b"]```',
    ],
)
def test_fence_is_stripped(text):
    assert extract_json(text) == ["a", "b"]
    assert _fixes(text) == frozenset({"fence"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["```json\\n[1]\\n```"]', ["```json\n[1]\n```"]),
        (
            '{"code": "```python\\nx = [1]\\n```", "n": 2}',
            {"code": "```python\nx = [1]\n```", "n": 2},
        ),
    ],
)
def test_whole_text_wins_over_fence_inside_string(text, expected):
    """全文本身能严格解析成符合 expect 的值时直接用全文，不去看字符串里的 ```。"""
    assert extract_json(text) == expected
    assert _fixes(text) == frozenset()


def test_fenced_block_wins_over_brackets_in_prose():
    text = '见 [附录] 说明：\n```json\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}
    assert _fixes(text) == frozenset({"fence"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('好的，结果如下：{"a": 1}。', {"a": 1}),
        ('Here you go:\n["x", "y"]\nHope this helps.', ["x", "y"]),
        ('他说"好的"，结果：["x"]', ["x"]),
        ('[注] 结果：{"a": 1}', {"a": 1}),
    ],
)
def test_json_is_extracted_from_surrounding_text(text, expected):
    assert extract_json(text) == expected
    assert _fixes(text) == frozenset({"surrounding_text"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["a", "b",]', ["a", "b"]),
        ('{"a": 1, }', {"a": 1}),
        ('{"a": [1, 2,\n],\n}', {"a": [1, 2]}),
    ],
)
def test_trailing_comma_is_dropped(text, expected):
    assert extract_json(text) == expected
    assert _fixes(text) == frozenset({"trailing_comma"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["a", b_c]', ["a", "b_c"]),
        ('{"kind": supplier}', {"kind": "supplier"}),
        ('{"kind": Supplier_Of}', {"kind": "Supplier_Of"}),
        ('["x", D8, D9]', ["x", "D8", "D9"]),
        ('{"a": "x", "b": __Mixed_Case__ }', {"a": "x", "b": "__Mixed_Case__"}),
    ],
)
def test_bare_value_is_quoted(text, expected):
    assert extract_json(text) == expected
    assert _fixes(text) == frozenset({"bare_value"})


def test_bare_value_example_from_review():
    """审阅用例：`["a", b_c]` 只能修成 `["a","b_c"]`。"""
    assert extract_json('["a", b_c]') == json.loads('["a","b_c"]')


@settings(derandomize=True, deadline=None)
@given(token=st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True))
def test_bare_value_quoting_keeps_token_verbatim(token):
    """补引号只能把 token 原样包成字符串：不改大小写、不去下划线、不改任何字符。"""
    if token.lower() in {"true", "false", "null", "none", "nan", "infinity", "undefined"}:
        assert extract_json(f'["x", {token}]') is None
        return
    assert extract_json(f'["x", {token}]') == ["x", token]
    assert extract_json(f'{{"k": {token}}}') == {"k": token}


def test_fixes_combine():
    text = (
        '好的：\n```json\n{"relations": [{"source": "A", "target": "B", "kind": supplier},],}\n```'
    )
    assert extract_json(text, expect="object") == {
        "relations": [{"source": "A", "target": "B", "kind": "supplier"}]
    }
    assert _fixes(text, "object") == frozenset({"fence", "trailing_comma", "bare_value"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["a,]", "b}",]', ["a,]", "b}"]),
        ('["x: y, z]", foo]', ["x: y, z]", "foo"]),
        ('["esc \\" , ]", "b",]', ['esc " , ]', "b"]),
    ],
)
def test_fixes_never_touch_string_literals(text, expected):
    assert extract_json(text) == expected


def test_trailing_comma_repair_keeps_outer_object():
    """外层对象只差一个尾逗号时修复外层，不退而返回里面的数组或对象。"""
    text = '{"relations": [{"source": "A", "target": "B", "kind": "k"}],}'
    assert extract_json(text, expect="object") == {
        "relations": [{"source": "A", "target": "B", "kind": "k"}]
    }
    assert extract_json(text) == {"relations": [{"source": "A", "target": "B", "kind": "k"}]}


# ---------------------------------------------------------------------------
# expect：只看顶层值，类型不符跳过整段
# ---------------------------------------------------------------------------


def test_expect_skips_mismatched_top_level_value():
    text = '先给对象 {"a": 1}，再给数组 ["x"]'
    assert extract_json(text, expect="array") == ["x"]
    assert extract_json(text, expect="object") == {"a": 1}
    assert extract_json(text) == {"a": 1}


def test_expect_array_does_not_look_inside_object():
    assert extract_json('{"subquestions": ["a"]}', expect="array") is None


def test_expect_object_does_not_look_inside_array():
    assert extract_json('[{"a": 1}]', expect="object") is None


# ---------------------------------------------------------------------------
# 反例：不做的修复一律 None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "无法分解该问题",
        "see [appendix]",
        "[a, b]",
        "[TODO] {placeholder}",
        "[link](http://x)",
        '["a", "b',
        "['a','b']",
        "{a: 1}",
        '{"a":1, b:2}',
        '["x", True]',
        '["x", None]',
        "[NaN]",
        '{"a": Infinity}',
        '{"a":1, // c\n"b":2}',
        "42",
        '"hello"',
        "true",
        '{"a":[1,2}',
        "[1,2,,3]",
        "[, 1]",
        "[,]",
        "[1,,]",
        '["x" "y"]',
        '{"k": v w}',
        '["x", undefined]',
        '["x", null_value w]',
    ],
)
def test_unrepairable_inputs_return_none(text):
    assert extract_json(text) is None


def test_bare_keyword_values_are_not_quoted():
    assert extract_json('["x", FALSE]') is None
    assert extract_json('["x", Null]') is None


# ---------------------------------------------------------------------------
# 健壮性：不抛、只返回容器、病态输入有界
# ---------------------------------------------------------------------------


@settings(derandomize=True, deadline=None)
@given(st.text())
def test_never_raises_and_returns_container_or_none(text):
    for expect in (None, "object", "array"):
        value = extract_json(text, expect=expect)
        assert value is None or isinstance(value, dict | list)


def _json_containers(strings: st.SearchStrategy[str]) -> st.SearchStrategy[Any]:
    scalars = (
        st.none()
        | st.booleans()
        | st.integers()
        | st.floats(allow_nan=False, allow_infinity=False)
        | strings
    )
    return st.recursive(
        scalars,
        lambda children: st.lists(children) | st.dictionaries(strings, children),
        max_leaves=20,
    ).filter(lambda v: isinstance(v, dict | list))


# 说明文字里不能有容器起点或反引号，否则它本身就是合法的"第一个 JSON"。
_PROSE = st.text(alphabet=st.characters(blacklist_characters="[{`"), max_size=40)


@settings(derandomize=True, deadline=None)
@given(value=_json_containers(st.text()))
def test_round_trip_whole_text(value):
    """全文能严格解析时直接用全文，字符串里含 ``` 也不会被当成围栏。"""
    dumped = json.dumps(value, ensure_ascii=False)
    assert extract_json(f"  {dumped}\n") == value
    assert _fixes(dumped) == frozenset()


# 带说明文字时全文不能严格解析，会按围栏优先：字符串里的 ``` 仍可能被当成围栏边界（已知局限），
# 所以这里的字符串不放反引号。
@settings(derandomize=True, deadline=None)
@given(
    value=_json_containers(st.text(alphabet=st.characters(blacklist_characters="`"))),
    prose=_PROSE,
    suffix=_PROSE,
)
def test_round_trip_with_prose_or_fence(value, prose, suffix):
    dumped = json.dumps(value, ensure_ascii=False)
    assert extract_json(f"{prose}{dumped}{suffix}") == value
    assert extract_json(f"{prose}\n```json\n{dumped}\n```\n{suffix}") == value


def test_many_open_brackets_is_bounded():
    text = "[" * 30_000
    start = time.perf_counter()
    assert extract_json(text) is None
    assert time.perf_counter() - start < _TIME_LIMIT_S


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_deep_nesting_returns_none(monkeypatch):
    """10 万层嵌套超过深度上限：返回 None，不返回结构；由深度上限判定，不碰 json.loads。"""
    depth = 100_000
    texts = [
        _nested(depth),
        '{"a": ' + _nested(depth) + "}",
        '["x", ' + "[" * depth + '"y"' + "]" * depth + ",]",
    ]

    def _no_loads(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("json.loads must not see over-deep input")

    monkeypatch.setattr(llm_json.json, "loads", _no_loads)
    for text in texts:
        start = time.perf_counter()
        assert extract_json(text) is None
        assert extract_json(text, expect="array") is None
        assert extract_json(text, expect="object") is None
        assert time.perf_counter() - start < _TIME_LIMIT_S


def test_max_depth_is_named_512():
    assert llm_json.MAX_JSON_DEPTH == 512


def test_depth_at_limit_parses_one_over_returns_none():
    limit = llm_json.MAX_JSON_DEPTH
    at_limit = json.loads(_nested(limit))
    assert extract_json(_nested(limit)) == at_limit
    assert parse_llm_json(_nested(limit), expect="array") == at_limit
    assert extract_json(_nested(limit + 1)) is None
    assert parse_llm_json(_nested(limit + 1), expect="array") is None
    obj_at_limit = '{"a": ' + _nested(limit - 1) + "}"
    assert extract_json(obj_at_limit) == json.loads(obj_at_limit)
    assert extract_json('{"a": ' + _nested(limit) + "}") is None


def test_deep_fenced_candidate_is_skipped_even_if_whole_text_scan_misses_it(monkeypatch):
    """全文扫描把围栏当成字符串内部时，围栏候选本身超深仍不交给 json.loads。"""
    deep = _nested(llm_json.MAX_JSON_DEPTH + 1)
    real_loads = json.loads

    def _guarded_loads(s: str, *args: Any, **kwargs: Any) -> Any:
        assert not s.strip().startswith("[" * (llm_json.MAX_JSON_DEPTH + 1))
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(llm_json.json, "loads", _guarded_loads)
    assert extract_json('"\n```json\n' + deep + "\n```\n") is None


def test_brackets_inside_strings_do_not_count_toward_depth():
    limit = llm_json.MAX_JSON_DEPTH
    noise = json.dumps("[{" * 5_000 + '\\"]')
    text = "[" * (limit - 1) + "[" + noise + "]" + "]" * (limit - 1)
    assert extract_json(text) == json.loads(text)
    assert parse_llm_json(text, expect="array") == json.loads(text)


# ---------------------------------------------------------------------------
# parse_llm_json：先按调用点原来的 strip + json.loads 解析，失败才走 extract_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '["a", NaN]',
        '  {"a": Infinity, "b": -Infinity}\n',
        "null",
        "42",
        '"hello"',
        '{"subquestions": ["a"]}',
        '{"a": 1, "a": 2}',
    ],
)
def test_parse_llm_json_keeps_whatever_json_loads_accepts(text):
    expected = json.loads(text.strip())
    for expect in (None, "object", "array"):
        got = parse_llm_json(text, expect=expect)
        assert json.dumps(got) == json.dumps(expected)


def test_parse_llm_json_falls_back_to_extract_json():
    assert parse_llm_json('好的：["a", "b",]', expect="array") == ["a", "b"]
    assert parse_llm_json("see [appendix]", expect="array") is None


def test_parse_llm_json_rejects_deep_nesting(monkeypatch):
    def _no_loads(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("json.loads must not see over-deep input")

    monkeypatch.setattr(llm_json.json, "loads", _no_loads)
    assert parse_llm_json(_nested(100_000), expect="array") is None


# ---------------------------------------------------------------------------
# 模块边界：只依赖 stdlib，不写 trace
# ---------------------------------------------------------------------------


def test_module_depends_on_stdlib_only():
    source = Path(llm_json.__file__).read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots
