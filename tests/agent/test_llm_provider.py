"""LLM provider 抽象测试：MockProvider 确定性行为 + AnthropicProvider 延迟 import / 网关适配。

全部离线：anthropic SDK 不装也必须全绿（fake module 注入 sys.modules）。
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

from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    MockProvider,
    ProviderResponse,
    ToolCall,
)
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_ANTHROPIC

REF = date(2026, 6, 12)
TOOLS = [QUERY_METRIC_TOOL_ANTHROPIC]
SYSTEM = "你是经营洞察助手。"


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _tool_result_messages(result: dict) -> list[dict]:
    """模拟 tool use 循环第二轮的消息序列（assistant tool_use + user tool_result）。"""
    return [
        _user("香港去年REVENUE多少"),
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": "toolu_1", "name": "query_metric",
                "input": {"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "toolu_1",
                "content": json.dumps(result, ensure_ascii=False),
            }],
        },
    ]


# ---------------------------------------------------------------------------
# MockProvider：确定性脚本化，零网络零 key
# ---------------------------------------------------------------------------

def test_mock_first_turn_emits_tool_use():
    provider = MockProvider(reference_date=REF)
    resp = provider.create_message(
        system=SYSTEM, messages=[_user("香港去年REVENUE多少")], tools=TOOLS
    )
    assert isinstance(resp, ProviderResponse)
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "query_metric"
    assert call.input["metric"] == "REVENUE"
    assert call.input["entity"] == "ACME_HK"
    assert call.input["period"] == "FY2025"  # 去年已用 reference_date 解析


def test_mock_final_answer_on_found():
    provider = MockProvider(reference_date=REF)
    found = {
        "status": "found", "value": 1702.0, "unit": "USD_M",
        "metric_code": "REVENUE", "entity": "ACME_HK", "geography": "HK",
        "channel": "TOTAL", "period_type": "FY", "period": "2025",
        "source": {"doc": "ACME_FY2025_Results.pptx", "locator": "slide=5,table=1"},
    }
    resp = provider.create_message(
        system=SYSTEM, messages=_tool_result_messages(found), tools=TOOLS
    )
    assert resp.tool_calls == []
    assert "1702" in resp.text
    assert "ACME_FY2025_Results.pptx" in resp.text
    assert "slide=5,table=1" in resp.text


def test_mock_final_answer_on_not_found_never_fabricates():
    provider = MockProvider(reference_date=REF)
    not_found = {
        "status": "not_found",
        "normalized": {"metric_code": "ROE", "entity": "ACME_CN",
                       "period_type": "FY", "period": "2024", "channel": "TOTAL"},
    }
    resp = provider.create_message(
        system=SYSTEM, messages=_tool_result_messages(not_found), tools=TOOLS
    )
    assert resp.tool_calls == []
    assert "查不到" in resp.text
    assert "ROE" in resp.text  # 回指查询口径，便于用户收窄
    assert "USD" not in resp.text  # 不带任何数值/单位，绝不编造


def test_mock_final_answer_on_unrecognized():
    provider = MockProvider(reference_date=REF)
    unrec = {"status": "unrecognized_param", "param": "metric", "raw": "净现金流量"}
    resp = provider.create_message(
        system=SYSTEM, messages=_tool_result_messages(unrec), tools=TOOLS
    )
    assert resp.tool_calls == []
    assert "无法识别" in resp.text
    assert "净现金流量" in resp.text


def test_mock_is_deterministic():
    p1 = MockProvider(reference_date=REF)
    p2 = MockProvider(reference_date=REF)
    msgs = [_user("香港去年REVENUE多少")]
    r1 = p1.create_message(system=SYSTEM, messages=msgs, tools=TOOLS)
    r2 = p2.create_message(system=SYSTEM, messages=msgs, tools=TOOLS)
    assert r1.tool_calls[0].input == r2.tool_calls[0].input
    assert r1.text == r2.text


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
    provider.create_message(system=SYSTEM, messages=[_user("hi")], tools=TOOLS)

    assert captured["client_kwargs"]["api_key"] == "sk-test"
    assert captured["client_kwargs"]["base_url"] == "https://genai-hub.acme.internal"


def test_anthropic_provider_default_model_used(monkeypatch):
    resp = _fake_response([SimpleNamespace(type="text", text="hi")], "end_turn")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="k")
    provider.create_message(system=SYSTEM, messages=[_user("hi")], tools=TOOLS)
    assert captured["create_kwargs"]["model"] == DEFAULT_ANTHROPIC_MODEL


def test_anthropic_provider_model_override(monkeypatch):
    resp = _fake_response([SimpleNamespace(type="text", text="hi")], "end_turn")
    captured = _install_fake_anthropic(monkeypatch, resp)

    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-6")
    provider.create_message(system=SYSTEM, messages=[_user("hi")], tools=TOOLS)
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
    out = provider.create_message(system=SYSTEM, messages=[_user("q")], tools=TOOLS)

    assert out.stop_reason == "tool_use"
    assert out.text == "我先查一下。"
    assert out.tool_calls == [
        ToolCall(id="toolu_abc", name="query_metric",
                 input={"metric": "REVENUE", "entity": "ACME_HK", "period": "FY2025"})
    ]
    assert out.raw_content is blocks  # 原样回传给下一轮 assistant 消息
    # tools 与 system 透传
    assert captured["create_kwargs"]["tools"] == TOOLS
    assert captured["create_kwargs"]["system"] == SYSTEM
