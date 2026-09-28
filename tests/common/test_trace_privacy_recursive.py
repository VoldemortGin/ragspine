"""隐私门递归检查（common/observability/sink.enforce_trace_privacy，ADR 0028）。

递归遍历 Mapping 的键、dataclass / NamedTuple 的字段名、list/tuple/set/frozenset 的元素（字符串不展开），
其余非标量对象 fail-closed 拒绝；报错时给出路径（如 `llm_calls[0].prompt`、`x{*}.prompt`）；
容器层数（含顶层载荷）超过 8 层按"可疑"报错，不截断放行。反证：只检查顶层的旧门对这些载荷一个都查不出。
"""

import enum
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import NamedTuple

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.observability import emit_trace
from ragspine.common.observability.sink import (
    FORBIDDEN_KEYS,
    MAX_TRACE_DEPTH,
    TraceError,
    enforce_trace_privacy,
)


class _Color(enum.Enum):
    RED = "red"


class _Code(enum.StrEnum):
    OK = "ok"


class _Level(enum.IntEnum):
    HIGH = 2


def _old_top_level_gate(fields: Mapping[str, object]) -> None:
    """反证 stub：引入递归前的旧门，只看顶层键。"""
    offending = sorted(k for k in fields if k.strip().lower() in FORBIDDEN_KEYS)
    if offending:
        raise TraceError(f"trace 载荷含受限字段 {offending}")


def _nested(levels: int) -> dict[str, object]:
    """含顶层在内共 `levels` 层容器的合法载荷（Mapping 与 list 交替）。"""
    inner: object = 1
    for i in range(levels - 1):
        inner = {"k": inner} if i % 2 else [inner]
    return {"root": inner}


_NESTED_LEAKS: tuple[tuple[dict[str, object], str], ...] = (
    ({"llm_calls": [{"stage": "synthesis", "prompt": "x"}]}, "llm_calls[0].prompt"),
    ({"page_images": {"text": "正文"}}, "page_images.text"),
    ({"rows": [[{"ok": 1}], [{"Answer ": "正文"}]]}, "rows[1][0].Answer "),
    ({"m": {"a": ({"b": {"CHUNK": "x"}},)}}, "m.a[0].b.CHUNK"),
)


@pytest.mark.parametrize(("payload", "path"), _NESTED_LEAKS)
def test_nested_forbidden_key_is_rejected_with_path(payload, path):
    with pytest.raises(TraceError) as exc_info:
        enforce_trace_privacy(payload)
    assert path in str(exc_info.value)


@pytest.mark.parametrize(("payload", "path"), _NESTED_LEAKS)
def test_emit_trace_rejects_nested_leak_before_logging(payload, path, caplog):
    with caplog.at_level(logging.INFO, logger="ragspine.trace"):
        with pytest.raises(TraceError, match=path.replace("[", r"\[").replace("]", r"\]")):
            emit_trace(None, request_id="r1", **payload)
    assert not [r for r in caplog.records if r.name == "ragspine.trace"]


@pytest.mark.parametrize(("payload", "path"), _NESTED_LEAKS)
def test_old_top_level_gate_misses_nested_leaks(payload, path):
    """反证：旧门对嵌套泄漏一个都查不出——新测试不是空泛通过。"""
    _old_top_level_gate(payload)


def test_depth_limit_is_eight():
    assert MAX_TRACE_DEPTH == 8


@pytest.mark.parametrize("levels", [1, 2, 7, 8])
def test_legal_payload_within_depth_passes(levels):
    enforce_trace_privacy(_nested(levels))


def test_nine_levels_is_rejected_as_suspicious():
    with pytest.raises(TraceError) as exc_info:
        enforce_trace_privacy(_nested(9))
    assert "root" in str(exc_info.value)
    _old_top_level_gate(_nested(9))  # 反证：旧门放行


def test_cycle_is_rejected_not_infinite():
    loop: list[object] = []
    loop.append(loop)
    with pytest.raises(TraceError):
        enforce_trace_privacy({"loop": loop})


def test_top_level_message_is_unchanged():
    with pytest.raises(TraceError) as exc_info:
        enforce_trace_privacy({"answer": "x", "Text": "y", "ok": 1})
    assert "['Text', 'answer']" in str(exc_info.value)


def test_strings_are_not_expanded_and_benign_nesting_passes():
    enforce_trace_privacy(
        {
            "route": "text",
            "chunk_ids": ["answer", "prompt"],
            "chunk_scores": [{"bm25": 1.0, "dense": 0.5}],
            "tool_status_counts": {"found": 1, "not_found": 0, "unrecognized": 0},
            "page_images": {"sent": 1, "dropped": 0, "dropped_reason": ""},
            "narrative_fallback": {"reason": "missing_metric", "grounded": True},
            "token_usage": {"input_tokens": 1, "output_tokens": 2},
            "llm_calls": [{"stage": "synthesis", "ms": 1, "error": ""}],
        }
    )


# ---------------------------------------------------------------------------
# 非 Mapping 容器（审阅补充）：dataclass / NamedTuple 按字段名当键检查；set / frozenset 逐元素（路径 `x{*}`）；
# 其余非标量对象（SimpleNamespace、普通对象、dataclass 类本身、未知类型）一律 fail-closed 拒绝并附路径。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LeakCall:
    stage: str
    prompt: str


@dataclass(frozen=True)
class _SafeCall:
    stage: str
    ms: int


class _LeakTuple(NamedTuple):
    stage: str
    prompt: str


class _SafeTuple(NamedTuple):
    stage: str
    ms: int


class _Plain:
    def __init__(self) -> None:
        self.prompt = "正文"


_CONTAINER_LEAKS: tuple[tuple[str, dict[str, object], str], ...] = (
    ("dataclass", {"x": _LeakCall("synthesis", "正文")}, "x.prompt"),
    (
        "dataclass_in_list",
        {"x": [_SafeCall("hyde", 1), _LeakCall("synthesis", "正文")]},
        "x[1].prompt",
    ),
    ("namedtuple", {"x": _LeakTuple("synthesis", "正文")}, "x.prompt"),
    ("set", {"x": {_LeakTuple("synthesis", "正文")}}, "x{*}.prompt"),
    ("frozenset", {"x": frozenset({_LeakCall("synthesis", "正文")})}, "x{*}.prompt"),
)

_UNKNOWN_OBJECTS: tuple[tuple[str, dict[str, object], str], ...] = (
    ("simple_namespace", {"x": SimpleNamespace(prompt="正文")}, "x"),
    ("plain_object", {"x": [_Plain()]}, "x[0]"),
    ("dataclass_class", {"x": _LeakCall}, "x"),
    ("object", {"x": {"y": object()}}, "x.y"),
    ("enum", {"x": _Color.RED}, "x"),
)


@pytest.mark.parametrize(
    ("kind", "payload", "path"), _CONTAINER_LEAKS, ids=[c[0] for c in _CONTAINER_LEAKS]
)
def test_non_mapping_container_leak_is_rejected_with_path(kind, payload, path):
    with pytest.raises(TraceError) as exc_info:
        emit_trace(None, request_id="r1", **payload)
    assert path in str(exc_info.value)
    _old_top_level_gate(payload)  # 反证：旧门放行


@pytest.mark.parametrize(
    ("kind", "payload", "path"), _UNKNOWN_OBJECTS, ids=[c[0] for c in _UNKNOWN_OBJECTS]
)
def test_unknown_object_is_rejected_fail_closed(kind, payload, path):
    with pytest.raises(TraceError, match="非标量") as exc_info:
        enforce_trace_privacy(payload)
    assert f"（{path}：" in str(exc_info.value)
    _old_top_level_gate(payload)  # 反证：旧门放行


def test_legal_dataclass_namedtuple_and_set_payloads_pass():
    enforce_trace_privacy(
        {
            "calls": [_SafeCall("hyde", 1), _SafeTuple("synthesis", 2)],
            "one": _SafeCall("tool_round", 3),
            "codes": {"a", "b"},
            "frozen": frozenset({1, 2}),
            "scalars": [None, True, 1, 1.5, "s", b"b"],
            "str_enum": _Code.OK,
            "int_enum": _Level.HIGH,
        }
    )


def test_container_depth_limit_counts_dataclass_and_set_levels():
    inner: object = _SafeCall("hyde", 1)  # dataclass 自身算一层
    for _ in range(6):
        inner = [inner]
    enforce_trace_privacy({"x": inner})  # 顶层 + 6 层 list + dataclass = 8 层
    with pytest.raises(TraceError, match="嵌套超过"):
        enforce_trace_privacy({"x": [inner]})
