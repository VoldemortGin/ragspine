"""OpenAICompatProvider 测试：全部经注入的 sender（SmokeSender）离线运行，零网络、零 SDK。"""

import json
from pathlib import Path

import pytest
from corespine import ChatCompletion, LLMProvider, ProviderError
from pydantic import SecretStr

from ragspine.agent.llm_provider import provider_supports_images
from ragspine.agent.openai_compat_provider import OpenAICompatProvider
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI
from ragspine.agent.truncation import TruncatedOutputError
from ragspine.common.evidence.providers.providers import LLMConfig, ProviderRequestError
from ragspine.common.observability.llm_calls import llm_stage, record_llm_calls
from ragspine.service.config import ServiceConfig, build_provider

CONFIG = LLMConfig(
    api_key=SecretStr("sk-test-secret"), base_url="https://llm.example.com", model="gpt-test"
)
TOOLS = [QUERY_METRIC_TOOL_OPENAI]


def _body(content="你好", *, tool_calls=None, finish="stop", usage=True) -> bytes:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict = {
        "id": "chatcmpl-1",
        "model": "gpt-test-0613",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    return json.dumps(body).encode()


class FakeSender:
    """记录每次调用，按脚本返回字节或抛出异常。"""

    def __init__(self, *outputs: bytes | Exception) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        self.calls.append({"url": url, "api_key": api_key, "payload": payload, "timeout": timeout})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    @property
    def request(self) -> dict:
        return json.loads(self.calls[-1]["payload"])


def _provider(*outputs: bytes | Exception, **kwargs) -> tuple[OpenAICompatProvider, FakeSender]:
    sender = FakeSender(*outputs)
    return OpenAICompatProvider(CONFIG, sender=sender, **kwargs), sender


def test_satisfies_llm_provider_protocol():
    provider, _ = _provider()
    assert isinstance(provider, LLMProvider)
    assert provider.model == "gpt-test"


def test_request_url_key_timeout_and_body_shape():
    provider, sender = _provider(_body(), timeout=12.5)
    provider.chat([{"role": "user", "content": "hi"}])
    call = sender.calls[0]
    assert call["url"] == "https://llm.example.com/v1/chat/completions"
    assert call["api_key"] == "sk-test-secret"
    assert call["timeout"] == 12.5
    assert sender.request == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }  # 无 tools / max_tokens 时不带这两个键


def test_max_tokens_and_tools_are_sent_when_set():
    provider, sender = _provider(_body(), max_tokens=256)
    provider.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert sender.request["max_tokens"] == 256
    assert sender.request["tools"] == TOOLS


def test_all_roles_pass_through_in_openai_shape():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "query_metric", "arguments": '{"a": 1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "found"}'},
    ]
    provider, sender = _provider(_body())
    provider.chat(messages)
    assert sender.request["messages"] == messages


def test_image_parts_become_image_url_when_declared(tmp_path: Path):
    png = tmp_path / "p1.png"
    png.write_bytes(b"\x89PNG-fake")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "path": str(png), "name": "p1.png", "doc_id": "d", "page": 1},
            ],
        }
    ]
    provider, sender = _provider(_body(), image_input=True)
    assert provider_supports_images(provider)
    provider.chat(messages)
    parts = sender.request["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "看图"}
    assert parts[1] == {"type": "text", "text": "p1.png"}
    assert parts[2]["type"] == "image_url"
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "path" not in json.dumps(sender.request)  # 本地路径不外发


def test_image_parts_flattened_to_text_when_not_declared(tmp_path: Path):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "只要文本"},
                {"type": "image", "path": str(tmp_path / "x.png"), "name": "x.png"},
            ],
        }
    ]
    provider, sender = _provider(_body())
    assert not provider_supports_images(provider)
    provider.chat(messages)
    assert sender.request["messages"][0]["content"] == "只要文本"


def test_parses_plain_text_reply_and_usage():
    provider, _ = _provider(_body("答案"))
    result = provider.chat([{"role": "user", "content": "q"}])
    assert isinstance(result, ChatCompletion)
    choice = result.choices[0]
    assert choice.message.content == "答案"
    assert choice.message.tool_calls is None
    assert choice.finish_reason == "stop"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (11, 7)
    assert result.usage.total_tokens == 18
    assert result.model == "gpt-test-0613" and result.id == "chatcmpl-1"


def test_usage_absent_is_none():
    provider, _ = _provider(_body(usage=False))
    assert provider.chat([{"role": "user", "content": "q"}]).usage is None


def test_parses_tool_calls_and_empty_arguments():
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "query_metric", "arguments": '{"metric": "REVENUE"}'},
        },
        {"id": "call_2", "type": "function", "function": {"name": "other", "arguments": ""}},
    ]
    provider, _ = _provider(_body(None, tool_calls=calls, finish="tool_calls"))
    choice = provider.chat([{"role": "user", "content": "q"}], tools=TOOLS).choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    first, second = choice.message.tool_calls
    assert (first.id, first.function.name, first.function.arguments) == (
        "call_1",
        "query_metric",
        '{"metric": "REVENUE"}',
    )
    assert second.function.arguments == "{}"


@pytest.mark.parametrize(
    "error",
    [
        ProviderRequestError("HTTP 500", status=500, category="http"),
        ProviderRequestError("timed out", category="timeout", exception_type="TimeoutError"),
        ProviderRequestError("conn", category="connection", exception_type="OSError"),
    ],
)
def test_upstream_errors_become_provider_error_without_secret(error):
    provider, _ = _provider(error)
    with pytest.raises(ProviderError) as info:
        provider.chat([{"role": "user", "content": "q"}])
    assert error.category in str(info.value)
    assert "sk-test-secret" not in str(info.value)
    assert isinstance(info.value.__cause__, ProviderRequestError)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": "x"}]}',
        b'{"choices": [{"message": {"content": 5}}]}',
        b'{"choices": [{"message": {"content": "a", "tool_calls": [{"id": "1"}]}}]}',
        b'{"choices": [{"message": {"content": "a"}}], "usage": {"prompt_tokens": "x"}}',
    ],
)
def test_malformed_response_is_typed_error_not_keyerror(raw):
    provider, _ = _provider(raw)
    with pytest.raises(ProviderError):
        provider.chat([{"role": "user", "content": "q"}])


def test_truncated_output_raises_and_does_not_retry():
    provider, sender = _provider(_body("半截", finish="length"), _body("不该被取用"))
    with pytest.raises(TruncatedOutputError):
        provider.chat([{"role": "user", "content": "q"}])
    assert len(sender.calls) == 1


def test_program_errors_propagate():
    provider, _ = _provider(KeyError("bug"))
    with pytest.raises(KeyError):
        provider.chat([{"role": "user", "content": "q"}])


def test_chat_is_instrumented_without_leaking_content():
    provider, _ = _provider(_body("机密答案"))
    with record_llm_calls() as calls, llm_stage("synthesis"):
        provider.chat([{"role": "user", "content": "机密问题"}])
    entries = calls.calls
    assert len(entries) == 1
    assert entries[0].stage == "synthesis" and entries[0].in_tokens == 11
    assert "机密" not in repr(entries[0]) and "sk-test" not in repr(entries[0])


def test_build_provider_openai_type_reads_app_llm_env(monkeypatch):
    monkeypatch.setenv("APP_LLM_API_KEY", "sk-env")
    monkeypatch.setenv("APP_LLM_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.setenv("APP_LLM_MODEL", "m1")
    provider = build_provider(ServiceConfig(db_path="/tmp/x.db", provider_type="openai"))
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "m1"
    assert provider.config.chat_completions_url == "https://gw.example.com/v1/chat/completions"


def test_build_provider_openai_type_missing_env_is_friendly(monkeypatch):
    from ragspine.common.evidence.providers.providers import ProviderConfigurationError

    for name in ("APP_LLM_API_KEY", "APP_LLM_BASE_URL", "APP_LLM_MODEL"):
        monkeypatch.setenv(name, "")
    with pytest.raises(ProviderConfigurationError, match="APP_LLM_API_KEY"):
        build_provider(ServiceConfig(db_path="/tmp/x.db", provider_type="openai"))
