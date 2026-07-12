"""node trace 采集净化：生成模块的 _NODE_TRACES（原始记录）→ JSON-safe 的对外 trace 列表。

生成代码在受限沙箱里逐节点写 _NODE_TRACES（emitter 的 emit_node_traces 注入）；那是
不受信的运行期产物——本模块把它净化成保证 json.dumps 可过的形状（NodeTrace 契约），
并赋 index（列表序）。净化规则：超长字符串截断、nan/inf 转 repr、不可序列化对象 repr
回退、深嵌套/自引用按最大深度降级，字段缺失给保守默认（status 越界一律归 'failed'）。
"""

from __future__ import annotations

import math
from typing import Any

# 单个字符串值的最大长度（超出截断并加省略标记）。
_MAX_STR_LEN = 2000
_TRUNCATION_MARK = "…(truncated)"
# inputs/outputs 递归净化的最大深度（超深降级 repr，防自引用环 / 递归爆栈）。
_MAX_DEPTH = 8

_STATUSES = frozenset({"succeeded", "failed", "skipped"})


def _sanitize_str(value: str) -> str:
    """超长字符串截断加省略标记；其余原样。"""
    if len(value) > _MAX_STR_LEN:
        return value[:_MAX_STR_LEN] + _TRUNCATION_MARK
    return value


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    """递归净化任意值为 JSON-safe：str 截断、非有限 float 转 repr、其它对象 repr 回退。"""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return _sanitize_str(value)
    if depth >= _MAX_DEPTH:
        return _sanitize_str(repr(value))
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v, depth + 1) for k, v in value.items()}
    return _sanitize_str(repr(value))


def _sanitize_mapping(value: Any) -> dict[str, Any] | None:
    """inputs/outputs 槽位：dict → 递归净化；None / 非 dict → None（保守默认）。"""
    if not isinstance(value, dict):
        return None
    sanitized = _sanitize_value(value)
    return sanitized if isinstance(sanitized, dict) else None


def _sanitize_elapsed_ms(value: Any) -> float:
    """elapsed_ms → 有限 float（bool / 非数 / nan / inf 一律 0.0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _sanitize_record(raw: Any, index: int) -> dict[str, Any]:
    """单条原始记录 → NodeTrace 契约形状（字段缺失给保守默认）。"""
    rec: dict[Any, Any] = raw if isinstance(raw, dict) else {}
    status = rec.get("status")
    if status not in _STATUSES:
        status = "failed"
    error = rec.get("error")
    return {
        "index": index,
        "node_id": _sanitize_str(str(rec.get("node_id", ""))),
        "title": _sanitize_str(str(rec.get("title", ""))),
        "node_type": _sanitize_str(str(rec.get("node_type", ""))),
        "status": status,
        "elapsed_ms": _sanitize_elapsed_ms(rec.get("elapsed_ms")),
        "inputs": _sanitize_mapping(rec.get("inputs")),
        "outputs": _sanitize_mapping(rec.get("outputs")),
        "error": None if error is None else _sanitize_str(str(error)),
    }


def sanitize_node_traces(raw: object) -> list[dict[str, Any]] | None:
    """把生成模块的 _NODE_TRACES 净化成 JSON-safe trace 列表；非 list → None。

    逐条净化并赋 index（列表序，skipped 记录本就由生成代码排在最后）。
    """
    if not isinstance(raw, list):
        return None
    return [_sanitize_record(item, index) for index, item in enumerate(raw)]
