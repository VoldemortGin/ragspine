"""node trace 采集净化：生成模块的 _NODE_TRACES（原始记录）→ JSON-safe 的对外 trace 列表。

生成代码在受限沙箱里逐节点写 _NODE_TRACES（emitter 的 emit_node_traces 注入）；那是
不受信的运行期产物——本模块把它净化成保证 json.dumps 可过的形状（NodeTrace 契约），
并赋 index（列表序）。净化规则：敏感键值递归脱敏、超长字符串截断、nan/inf 转 repr、
不可序列化对象 repr 回退、深嵌套/自引用按最大深度降级为无内容占位，字段缺失给保守默认
（status 越界一律归 'failed'）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# 单个字符串值的最大长度（超出截断并加省略标记）。
_MAX_STR_LEN = 2000
_TRUNCATION_MARK = "…(truncated)"
_REDACTION_MARK = "[REDACTED]"
_DEPTH_LIMIT_MARK = "…(max depth)"
# inputs/outputs 递归净化的最大深度（超深降级为无内容占位，防自引用环 / 递归爆栈）。
_MAX_DEPTH = 8

# 去掉大小写与常见分隔符后按后缀匹配，使 openai_api_key / DB-PASSWORD 等命名也能命中；
# 只匹配后缀，避免 token_count / secret_length 这类普通元数据被误伤。
_SENSITIVE_KEY_SUFFIXES = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authtoken",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "awssecuritytoken",
        "awssessiontoken",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "token",
        "webhooksecret",
    }
)

_STATUSES = frozenset({"succeeded", "failed", "skipped"})


def _sanitize_str(value: str) -> str:
    """超长字符串截断加省略标记；其余原样。"""
    if len(value) > _MAX_STR_LEN:
        return value[:_MAX_STR_LEN] + _TRUNCATION_MARK
    return value


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否表示凭据；忽略大小写及空白/连字符/下划线/点等分隔符。"""
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    """递归净化为 JSON-safe 值，并在 mapping 边界按敏感键脱敏。"""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return _sanitize_str(value)
    if isinstance(value, Mapping):
        if depth >= _MAX_DEPTH:
            return _DEPTH_LIMIT_MARK
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            sanitized[key] = (
                _REDACTION_MARK if _is_sensitive_key(key) else _sanitize_value(raw_value, depth + 1)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_DEPTH:
            return _DEPTH_LIMIT_MARK
        return [_sanitize_value(v, depth + 1) for v in value]
    return _sanitize_str(repr(value))


def _sanitize_mapping(value: Any) -> dict[str, Any] | None:
    """inputs/outputs 槽位：mapping → 递归净化；其它类型 → None。"""
    if not isinstance(value, Mapping):
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
