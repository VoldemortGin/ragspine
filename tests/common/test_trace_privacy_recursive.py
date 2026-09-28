"""隐私门递归检查（common/observability/sink.enforce_trace_privacy，ADR 0028）。

递归遍历 Mapping 的键与 list/tuple 的元素（字符串不展开），报错时给出路径（如 `llm_calls[0].prompt`）；
容器层数（含顶层载荷）超过 8 层按"可疑"报错，不截断放行。反证：只检查顶层的旧门对这些载荷一个都查不出。
"""

import logging
import os
from collections.abc import Mapping

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
