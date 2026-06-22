"""LLM provider 抽象测试：MockProvider 确定性行为 + AnthropicProvider 延迟 import / 网关适配。

全部离线：anthropic SDK 不装也必须全绿（fake module 注入 sys.modules）。
provider 实现 corespine 的 chat 缝（OpenAI chat-completions 规范）。
"""

import json
import os
import sys
import types
from datetime import date
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion
from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    MockProvider,
)
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI

REF = date(2026, 6, 12)
TOOLS = [QUERY_METRIC_TOOL_OPENAI]
SYSTEM = "你是经营洞察助手。"


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _tool_result_messages(result: dict) -> list[dict]:
    """模拟 tool use 循环第二轮的消息序列（OpenAI 形状：user + assistant tool_calls + tool 结果）。"""
    return [
        _user("香港去年REVENUE多少"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "query_metric",
                    "arguments": json.dumps(
                        {"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"}
                    ),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": json.dumps(result, ensure_ascii=False),
        },
    ]


# ---------------------------------------------------------------------------
# MockProvider：确定性脚本化，零网络零 key
# ---------------------------------------------------------------------------

def test_mock_first_turn_emits_tool_use():
    provider = MockProvider(reference_date=REF)
    resp = provider.chat(
        [{"role": "system", "content": SYSTEM}, _user("香港去年REVENUE多少")], tools=TOOLS
    )
    assert isinstance(resp, ChatCompletion)
    tool_calls = resp.choices[0].message.tool_calls
    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert call.function.name == "query_metric"
    args = json.loads(call.function.arguments)
    assert args["metric"] == "REVENUE"
    assert args["entity"] == "ACME_HK"
    assert args["period"] == "FY2025"  # 去年已用 reference_date 解析


def test_mock_final_answer_on_found():
    provider = MockProvider(reference_date=REF)
    found = {
        "status": "found", "value": 1702.0, "unit": "USD_M",
        "metric_code": "REVENUE", "entity": "ACME_HK", "geography": "HK",
        "channel": "TOTAL", "period_type": "FY", "period": "2025",
        "source": {"doc": "ACME_FY2025_Results.pptx", "locator": "slide=5,table=1"},
    }
    resp = provider.chat(
        [{"role": "system", "content": SYSTEM}, *_tool_result_messages(found)], tools=TOOLS
    )
    assert resp.choices[0].message.tool_calls is None
    text = resp.choices[0].message.content
    assert "1702" in text
    assert "ACME_FY2025_Results.pptx" in text
    assert "slide=5,table=1" in text


def test_mock_final_answer_on_not_found_never_fabricates():
    provider = MockProvider(reference_date=REF)
    not_found = {
        "status": "not_found",
        "normalized": {"metric_code": "ROE", "entity": "ACME_CN",
                       "period_type": "FY", "period": "2024", "channel": "TOTAL"},
    }
    resp = provider.chat(
        [{"role": "system", "content": SYSTEM}, *_tool_result_messages(not_found)], tools=TOOLS
    )
    assert resp.choices[0].message.tool_calls is None
    text = resp.choices[0].message.content
    assert "查不到" in text
    assert "ROE" in text  # 回指查询口径，便于用户收窄
    assert "USD" not in text  # 不带任何数值/单位，绝不编造


def test_mock_final_answer_on_unrecognized():
    provider = MockProvider(reference_date=REF)
    unrec = {"status": "unrecognized_param", "param": "metric", "raw": "净现金流量"}
    resp = provider.chat(
        [{"role": "system", "content": SYSTEM}, *_tool_result_messages(unrec)], tools=TOOLS
    )
    assert resp.choices[0].message.tool_calls is None
    text = resp.choices[0].message.content
    assert "无法识别" in text
    assert "净现金流量" in text


def test_mock_is_deterministic():
    p1 = MockProvider(reference_date=REF)
    p2 = MockProvider(reference_date=REF)
    msgs = [{"role": "system", "content": SYSTEM}, _user("香港去年REVENUE多少")]
    r1 = p1.chat(msgs, tools=TOOLS)
    r2 = p2.chat(msgs, tools=TOOLS)
    args1 = json.loads(r1.choices[0].message.tool_calls[0].function.arguments)
    args2 = json.loads(r2.choices[0].message.tool_calls[0].function.arguments)
    assert args1 == args2
    assert r1.choices[0].message.content == r2.choices[0].message.content


# ---------------------------------------------------------------------------
# AnthropicProvider：延迟 import、默认模型集中、base_url 可覆盖
# ---------------------------------------------------------------------------

def test_default_model_constant_centralized():
    assert DEFAULT_ANTHROPIC_MODEL == "claude-opus-4-8"


def test_module_importable_and_ctor_fails_without_sdk(monkeypatch):
    """SDK 延迟 import：模块可导入；缺 anthropic 时构造报带提示的错误。"""
    monkeypatch.setitem(sys.modules, "anthropic", None)  # 模拟未安装
    with pytest.raises((ImportError, RuntimeError)) as exc_info:
        AnthropicProvider(api_key="k")
    assert "anthropic" in str(exc_info.value).lower()


def _install_fake_anthropic(monkeypatch, response):
    """注入 fake anthropic 模块，捕获构造参数与请求参数。"""
    captured: dict = {}

    class _Messages:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return response

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return captured


def _fake_response(blocks, stop_reason):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def test_anthropic_provider_passes_base_url_and_key(monkeypatch):
    resp = _fake_response([SimpleNamespace(type="text", text="hi")], "end_turn")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="sk-test", base_url="https://genai-hub.acme.internal")
    provider.chat([{"role": "system", "content": SYSTEM}, _user("hi")], tools=TOOLS)

    assert captured["client_kwargs"]["api_key"] == "sk-test"
    assert captured["client_kwargs"]["base_url"] == "https://genai-hub.acme.internal"


def test_anthropic_provider_default_model_used(monkeypatch):
    resp = _fake_response([SimpleNamespace(type="text", text="hi")], "end_turn")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="k")
    provider.chat([{"role": "system", "content": SYSTEM}, _user("hi")], tools=TOOLS)
    assert captured["create_kwargs"]["model"] == DEFAULT_ANTHROPIC_MODEL


def test_anthropic_provider_model_override(monkeypatch):
    resp = _fake_response([SimpleNamespace(type="text", text="hi")], "end_turn")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-6")
    provider.chat([{"role": "system", "content": SYSTEM}, _user("hi")], tools=TOOLS)
    assert captured["create_kwargs"]["model"] == "claude-sonnet-4-6"


def test_anthropic_provider_converts_tool_use_blocks(monkeypatch):
    blocks = [
        SimpleNamespace(type="text", text="我先查一下。"),
        SimpleNamespace(
            type="tool_use", id="toolu_abc", name="query_metric",
            input={"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"},
        ),
    ]
    resp = _fake_response(blocks, "tool_use")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="k")
    out = provider.chat([{"role": "system", "content": SYSTEM}, _user("q")], tools=TOOLS)

    assert out.choices[0].finish_reason == "tool_calls"  # tool_use → tool_calls 映射
    assert out.choices[0].message.content == "我先查一下。"
    tcs = out.choices[0].message.tool_calls
    assert len(tcs) == 1
    assert tcs[0].id == "toolu_abc"
    assert tcs[0].function.name == "query_metric"
    assert json.loads(tcs[0].function.arguments) == {
        "metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"
    }
    # tools 内部转成 Anthropic 形状再传 SDK；system 角色合并成 system 字符串透传。
    tools_arg = captured["create_kwargs"]["tools"]
    assert tools_arg[0]["name"] == "query_metric"
    assert "input_schema" in tools_arg[0]
    assert captured["create_kwargs"]["system"] == SYSTEM
