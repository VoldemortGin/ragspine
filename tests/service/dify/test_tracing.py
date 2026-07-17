"""sanitize_node_traces 单测：JSON-safe 净化（截断 / repr 回退 / nan / 深嵌套 / index）。"""

import json
import math

from ragspine.service.dify.tracing import sanitize_node_traces


def _record(**overrides):
    base = {
        "node_id": "n1",
        "title": "标题",
        "node_type": "llm",
        "status": "succeeded",
        "elapsed_ms": 1.5,
        "inputs": {"a.b": "x"},
        "outputs": {"text": "y"},
        "error": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 非法输入 / index
# ---------------------------------------------------------------------------
def test_non_list_returns_none():
    assert sanitize_node_traces(None) is None
    assert sanitize_node_traces({"node_id": "x"}) is None
    assert sanitize_node_traces("not a list") is None


def test_index_assigned_in_list_order():
    out = sanitize_node_traces([_record(node_id="a"), _record(node_id="b")])
    assert [t["index"] for t in out] == [0, 1]
    assert [t["node_id"] for t in out] == ["a", "b"]


def test_record_fields_preserved():
    out = sanitize_node_traces([_record()])
    t = out[0]
    assert t["title"] == "标题"
    assert t["node_type"] == "llm"
    assert t["status"] == "succeeded"
    assert t["elapsed_ms"] == 1.5
    assert t["inputs"] == {"a.b": "x"}
    assert t["outputs"] == {"text": "y"}
    assert t["error"] is None


def test_invalid_status_coerced_to_failed():
    out = sanitize_node_traces([_record(status="weird"), _record(status=None)])
    assert all(t["status"] == "failed" for t in out)


def test_missing_fields_get_conservative_defaults():
    out = sanitize_node_traces([{}])
    t = out[0]
    assert t["node_id"] == ""
    assert t["status"] == "failed"
    assert t["elapsed_ms"] == 0.0
    assert t["inputs"] is None
    assert t["outputs"] is None
    assert t["error"] is None


# ---------------------------------------------------------------------------
# 值净化：截断 / 不可序列化 / nan / 深嵌套 —— 净化后必须 json.dumps 可过
# ---------------------------------------------------------------------------
def test_long_string_truncated():
    out = sanitize_node_traces([_record(inputs={"k": "x" * 2500})])
    v = out[0]["inputs"]["k"]
    assert v.endswith("…(truncated)")
    assert len(v) == 2000 + len("…(truncated)")


def test_sensitive_values_redacted_recursively():
    out = sanitize_node_traces(
        [
            _record(
                inputs={
                    "api_key": "sk-input-secret",
                    "nested": {
                        "PASSWORD": "correct horse battery staple",
                        "items": [
                            {"client-secret": "client-secret-value"},
                            {"session.cookie": "session-cookie-value"},
                        ],
                    },
                },
                outputs={
                    "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
                    "refresh_token": "refresh-token-value",
                },
            )
        ]
    )

    assert out[0]["inputs"] == {
        "api_key": "[REDACTED]",
        "nested": {
            "PASSWORD": "[REDACTED]",
            "items": [
                {"client-secret": "[REDACTED]"},
                {"session.cookie": "[REDACTED]"},
            ],
        },
    }
    assert out[0]["outputs"] == {
        "Authorization": "[REDACTED]",
        "refresh_token": "[REDACTED]",
    }


def test_sensitive_key_matching_handles_case_separators_and_prefixes():
    secrets = {
        "ApiKey": "a",
        "OPENAI-API-KEY": "b",
        "auth.token": "c",
        "access token": "d",
        "db_password": "e",
        "webhookSecret": "f",
        "Private-Key": "g",
        "credentials": "h",
    }

    out = sanitize_node_traces([_record(inputs=secrets)])

    assert set(out[0]["inputs"].values()) == {"[REDACTED]"}


def test_non_secret_metadata_and_normal_values_are_preserved():
    normal = {
        "token_count": 12,
        "secret_length": 32,
        "cookie_policy": "strict",
        "authorization_mode": "oauth2",
        "password_hint": "managed externally",
        "nested": [{"message": "Bearer is an HTTP authorization scheme"}],
    }

    out = sanitize_node_traces([_record(inputs=normal)])

    assert out[0]["inputs"] == normal


def test_deep_sensitive_value_never_leaks_through_depth_fallback():
    deep = {"api_key": "deep-secret-value"}
    for index in range(20):
        deep = {f"level_{index}": deep}

    out = sanitize_node_traces([_record(inputs={"deep": deep})])
    serialized = json.dumps(out)

    assert "deep-secret-value" not in serialized


def test_unserializable_object_becomes_repr():
    class Weird:
        def __repr__(self) -> str:
            return "<Weird>"

    out = sanitize_node_traces([_record(outputs={"o": Weird()})])
    assert out[0]["outputs"]["o"] == "<Weird>"
    json.dumps(out)  # 必须 JSON-safe


def test_nan_and_inf_become_strings():
    out = sanitize_node_traces([_record(inputs={"a": float("nan"), "b": float("inf"), "c": 1.5})])
    ins = out[0]["inputs"]
    assert ins["a"] == "nan"
    assert ins["b"] == "inf"
    assert ins["c"] == 1.5
    json.dumps(out)


def test_nan_elapsed_ms_coerced():
    out = sanitize_node_traces([_record(elapsed_ms=float("nan"))])
    assert out[0]["elapsed_ms"] == 0.0
    assert not math.isnan(out[0]["elapsed_ms"])


def test_deep_nesting_degrades_to_repr():
    deep = "leaf"
    for _ in range(20):
        deep = {"d": deep}
    out = sanitize_node_traces([_record(inputs={"deep": deep})])
    json.dumps(out)  # 不递归爆栈，且 JSON-safe
    # 超深层级被降级成 repr 字符串（不再是 dict 全展开）。
    node = out[0]["inputs"]["deep"]
    depth = 0
    while isinstance(node, dict):
        node = node["d"]
        depth += 1
    assert isinstance(node, str)
    assert depth < 20


def test_self_referential_structure_does_not_hang():
    loop: list = []
    loop.append(loop)
    out = sanitize_node_traces([_record(inputs={"loop": loop})])
    json.dumps(out)


def test_non_dict_inputs_outputs_become_none():
    out = sanitize_node_traces([_record(inputs="not a dict", outputs=[1, 2])])
    assert out[0]["inputs"] is None
    assert out[0]["outputs"] is None


def test_dict_keys_coerced_to_str():
    out = sanitize_node_traces([_record(inputs={1: "a", ("t",): "b"})])
    assert all(isinstance(k, str) for k in out[0]["inputs"])
    json.dumps(out)


def test_error_coerced_to_str_and_truncated():
    out = sanitize_node_traces([_record(error="E" * 3000), _record(error=123)])
    assert out[0]["error"].endswith("…(truncated)")
    assert out[1]["error"] == "123"
